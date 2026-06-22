"""Trading Engine Orchestrator — wires all components together.

Pipeline:
    FeedHandler.on_bar -> AlphaEngine.generate_signal -> RiskManager.validate
    -> PortfolioManager.allocate -> OrderManager.submit

Also handles:
    - Warm-up (historical data fetch)
    - Daily P&L tracking
    - Trade pivot CSV export
    - Position monitoring (stop-loss/target/force-exit)
"""

import asyncio
import os
from typing import TYPE_CHECKING, Optional

import structlog

if TYPE_CHECKING:
    from strader3.adapters.base import DataFeeder

from strader3.core.alpha_engine import BaseStrategy, STRsiTrendRider
from strader3.core.feed_handler import FeedHandler
from strader3.core.order_manager import OrderManager
from strader3.core.portfolio_manager import CapitalAllocator, PortfolioManager
from strader3.core.repository import (
    DailyPnlRepository,
    MigrationManager,
    PositionRepository,
    SignalRepository,
    TradeRepository,
)
from strader3.core.risk_manager import RiskManager
from strader3.models import BarData, Signal, SignalType
from strader3.models.trading import Position
from strader3.utils.config import get_nested
from strader3.utils.trade_pivot import export_trades_csv

logger = structlog.get_logger(__name__)


class TradingEngine:
    """Main trading engine orchestrator.

    Wires the full pipeline:
        FeedHandler -> AlphaEngine -> RiskManager -> PortfolioManager -> OrderManager
    """

    def __init__(
        self,
        config: dict,
        feeder: Optional["DataFeeder"] = None,
        db_path: str | None = None,
    ) -> None:
        self.config = config
        self._running = False
        self._warm_up_complete = False

        # --- Database ---
        self.db_path = db_path or get_nested(config, "database.path", "data/trades.db")
        self._ensure_db_dir()

        # --- Broker feeder ---
        self.feeder = feeder

        # --- Strategy ---
        self.strategy = self._init_strategy(config)

        # --- Risk Manager ---
        self.risk_manager = self._init_risk_manager(config)

        # --- Portfolio Manager ---
        allocator = self._init_capital_allocator(config)
        sector_map = get_nested(config, "sectors", {})
        cooldown = get_nested(config, "portfolio.cooldown_seconds", 300)
        self.portfolio_manager = PortfolioManager(
            capital_allocator=allocator,
            sector_map=sector_map,
            cooldown_seconds=cooldown,
        )

        # --- Order Manager ---
        paper_trade = get_nested(config, "trade_mode", "paper") == "paper"
        self.order_manager = OrderManager(
            db_path=self.db_path,
            paper_trade=paper_trade,
        )

        # --- Feed Handler ---
        if self.feeder is not None:
            self.feed_handler = FeedHandler(
                feeder=self.feeder,
                bar_interval=get_nested(config, "market_data.bar_interval", "1m"),
            )
        else:
            self.feed_handler = None

        # --- Repositories ---
        self.signal_repo = SignalRepository(self.db_path)
        self.position_repo = PositionRepository(self.db_path)
        self.trade_repo = TradeRepository(self.db_path)
        self.daily_pnl_repo = DailyPnlRepository(self.db_path)

        # --- Wire callbacks ---
        self._setup_callbacks()

        # --- State ---
        self._daily_pnl_updated = False
        self._last_csv_export_date: str | None = None

        logger.info(
            "trading_engine.initialized",
            db_path=self.db_path,
            paper_trade=paper_trade,
            strategy=self.strategy.name if self.strategy else "none",
        )

    def _ensure_db_dir(self) -> None:
        """Ensure database directory exists."""
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    def _init_strategy(self, config: dict) -> BaseStrategy | None:
        """Initialize strategy from config."""
        strategy_config = get_nested(config, "strategies.st_rsi_trend_rider", {})
        if not strategy_config.get("enabled", False):
            logger.warning("trading_engine.no_strategy_enabled")
            return None

        return STRsiTrendRider(strategy_config)

    def _init_risk_manager(self, config: dict) -> RiskManager:
        """Initialize risk manager from config."""
        risk_cfg = get_nested(config, "risk", {})
        rm = RiskManager(
            max_daily_loss_pct=risk_cfg.get("max_daily_loss_pct", 2.0),
            per_trade_allocation_pct=5.0,
            max_open_positions=3,
            entry_start=risk_cfg.get("entry_start_time", "09:20"),
            entry_end=risk_cfg.get("entry_end_time", "15:00"),
            force_exit_time=risk_cfg.get("force_exit_time", "15:15"),
            max_order_value=risk_cfg.get("max_order_value", 50_000.0),
            max_quantity=risk_cfg.get("max_quantity_per_order", 1000),
            total_exposure_cap_pct=risk_cfg.get("max_total_exposure_pct", 50.0),
        )

        # Set capital from portfolio config
        total_capital = get_nested(config, "portfolio.allocation_pct", 50.0) * 100_000
        rm.set_capital(total_capital)
        return rm

    def _init_capital_allocator(self, config: dict) -> CapitalAllocator:
        """Initialize capital allocator from config."""
        portfolio_cfg = get_nested(config, "portfolio", {})
        return CapitalAllocator(
            total_capital=portfolio_cfg.get("allocation_pct", 50.0) * 100_000,
            allocation_pct=portfolio_cfg.get("allocation_pct", 5.0),
            max_active_trades=portfolio_cfg.get("max_active_trades", 3),
            max_sector_exposure_pct=portfolio_cfg.get("max_sector_exposure_pct", 30.0),
            sizing_mode=portfolio_cfg.get("sizing_mode", "ALLOCATION_BASED"),
        )

    def _setup_callbacks(self) -> None:
        """Wire all component callbacks."""
        if self.feed_handler is not None and self.strategy is not None:
            self.feed_handler.on_bar(self._on_bar)

        if self.strategy is not None:
            self.strategy.on_signal(self._on_signal)

        # Order fill -> Portfolio Manager
        self.order_manager._fill_callbacks.append(self._on_order_fill)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_bar(self, bar: BarData) -> None:
        """Handle new bar from feed handler."""
        if self.strategy is None:
            return

        # Update portfolio MTM
        self.portfolio_manager.update_mtm(bar.symbol, bar.close)

        # Feed to strategy
        self.strategy.on_bar(bar)

        # Check stop-loss / target for existing positions
        self._check_position_exits(bar)

    def _on_signal(self, signal: Signal) -> None:
        """Handle signal from strategy -> Risk validation -> Portfolio -> Orders."""
        logger.info(
            "trading_engine.signal_received",
            symbol=signal.symbol,
            type=signal.signal_type.value,
            price=signal.ltp,
        )

        # Persist signal
        asyncio.create_task(self.signal_repo.save(signal))

        # Risk validation
        current_exposure = self.portfolio_manager.allocator._used_capital
        open_positions = len(self.portfolio_manager.get_all_positions())

        approved, reason = self.risk_manager.validate_signal(
            signal, current_exposure, open_positions
        )

        if not approved:
            signal.rms_approved = False
            signal.rms_rejection_reason = reason
            logger.info(
                "trading_engine.signal_rejected",
                symbol=signal.symbol,
                reason=reason,
            )
            return

        signal.rms_approved = True

        # Forward to portfolio manager
        asyncio.create_task(self._process_portfolio_signal(signal))

    async def _process_portfolio_signal(self, signal: Signal) -> None:
        """Process signal through portfolio manager."""
        approved = await self.portfolio_manager.process_signal(signal)
        if not approved:
            return

        # Forward approved signal to order manager
        order = await self.order_manager.process_signal(signal)
        if order is not None:
            logger.info(
                "trading_engine.order_submitted",
                order_id=order.id,
                symbol=order.symbol,
                quantity=order.quantity,
            )

    def _on_order_fill(
        self, symbol: str, quantity: int, price: float, is_entry: bool
    ) -> None:
        """Handle order fill notification."""
        self.portfolio_manager.on_order_filled(symbol, quantity, price, is_entry)

        # Update position repo
        position = self.portfolio_manager.get_position(symbol)
        if position:
            asyncio.create_task(self.position_repo.save(position))

    def _check_position_exits(self, bar: BarData) -> None:
        """Check if any position hits stop-loss or target."""
        position = self.portfolio_manager.get_position(bar.symbol)
        if position is None or position.quantity == 0:
            return

        if RiskManager.check_stop_loss(position, bar.close):
            self._emit_exit_signal(position, "stop_loss")
        elif RiskManager.check_target(position, bar.close):
            self._emit_exit_signal(position, "target")

    def _emit_exit_signal(self, position: Position, reason: str) -> None:
        """Emit exit signal for a position."""
        sig_type = (
            SignalType.EXIT_LONG
            if position.quantity > 0
            else SignalType.EXIT_SHORT
        )
        signal = Signal(
            symbol=position.symbol,
            signal_type=sig_type,
            ltp=position.current_price,
            quantity=abs(position.quantity),
            reason=reason,
        )
        logger.info(
            "trading_engine.exit_signal",
            symbol=position.symbol,
            reason=reason,
            price=position.current_price,
        )
        asyncio.create_task(self._process_portfolio_signal(signal))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize all components (DB, strategy warm-up)."""
        logger.info("trading_engine.initializing")

        # Run migrations
        migration_mgr = MigrationManager(self.db_path)
        await migration_mgr.migrate()

        # Initialize order manager
        await self.order_manager.initialize()

        # Set capital
        total_capital = get_nested(self.config, "portfolio.allocation_pct", 50.0) * 100_000
        self.risk_manager.set_capital(total_capital)

        # Warm up strategy
        if self.strategy is not None and self.feed_handler is not None and self.feeder is not None:
            await self._warm_up()

        self._running = True
        logger.info("trading_engine.initialized")

    async def _warm_up(self) -> None:
        """Warm up strategy with historical data."""
        if self._warm_up_complete:
            return

        watchlist = get_nested(self.config, "market_data.watchlist", [])
        if not watchlist:
            logger.warning("trading_engine.no_watchlist")
            return

        lookback = get_nested(self.config, "strategies.warm_up.lookback_bars", 100)
        timeframe = get_nested(self.config, "strategies.warm_up.timeframe", "5m")

        logger.info(
            "trading_engine.warm_up_starting",
            symbols=len(watchlist),
            lookback_bars=lookback,
            timeframe=timeframe,
        )

        if self.feeder is not None and self.strategy is not None:
            await self.strategy.warm_up(watchlist, self.feeder, lookback, timeframe)
        self._warm_up_complete = True
        logger.info("trading_engine.warm_up_complete")

    async def run(self) -> None:
        """Start the trading engine (main loop)."""
        await self.initialize()

        if self.feed_handler is None:
            logger.info("trading_engine.no_feeder_running_dry")
            logger.info("trading_engine.started_no_feed")
            # Keep running for health checks
            try:
                while self._running:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
            return

        watchlist = get_nested(self.config, "market_data.watchlist", [])
        logger.info("trading_engine.starting_feed", symbols=len(watchlist))

        try:
            await self.feed_handler.start(watchlist)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("trading_engine.feed_error")
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("trading_engine.shutting_down")
        self._running = False

        # Export trade pivot CSV
        await self._export_trade_pivot()

        # Stop feed handler
        if self.feed_handler is not None:
            try:
                await self.feed_handler.stop()
            except Exception:
                logger.exception("trading_engine.feed_stop_error")

        logger.info("trading_engine.shutdown_complete")

    # ------------------------------------------------------------------
    # Monitoring & reporting
    # ------------------------------------------------------------------

    async def _export_trade_pivot(self) -> None:
        """Export trade pivot CSV."""
        pivot_path = get_nested(
            self.config, "strategies.trade_pivot_log.path", "data/logs/trade_pivots.csv"
        )
        try:
            exported = await export_trades_csv(self.db_path, pivot_path)
            logger.info("trading_engine.pivot_exported", path=exported)
        except Exception:
            logger.exception("trading_engine.pivot_export_error")

    def get_status(self) -> dict:
        """Get engine status snapshot."""
        return {
            "running": self._running,
            "warm_up_complete": self._warm_up_complete,
            "strategy": self.strategy.name if self.strategy else None,
            "strategy_enabled": self.strategy.enabled if self.strategy else False,
            "risk_metrics": self.risk_manager.get_metrics(),
            "portfolio_metrics": self.portfolio_manager.get_metrics(),
            "order_metrics": self.order_manager.get_metrics(),
            "db_path": self.db_path,
        }

    def stop(self) -> None:
        """Signal the engine to stop."""
        self._running = False
