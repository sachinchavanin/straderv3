"""Trading Engine Orchestrator — wires all components together.

Pipeline:
    FeedHandler.on_bar -> AlphaEngine.generate_signal -> RiskManager.validate
    -> PortfolioManager.allocate -> OrderManager.submit

Also handles:
    - Warm-up (historical data fetch)
    - Daily P&L tracking
    - Trade pivot CSV export
    - Position monitoring (stop-loss/target/force-exit)
    - Discord alerts via AlertManager
    - Paper-trade validation logging
    - Market phase awareness and time guards
    - Config-driven watchlist with hot-reload
"""

import asyncio
import os
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import structlog

if TYPE_CHECKING:
    from strader3.adapters.base import DataFeeder

try:
    from strader3.core.alpha_engine import BaseStrategy, STRsiTrendRider
except ImportError:
    BaseStrategy = None  # type: ignore
    STRsiTrendRider = None  # type: ignore
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
from strader3.notifier import (
    AlertManager,
    MarketPhaseChecker,
    PaperTradeValidator,
    WatchlistManager,
)
from strader3.utils.config import get_nested
from strader3.utils.trade_pivot import export_trades_csv

logger = structlog.get_logger(__name__)


class TradingEngine:
    """Main trading engine orchestrator."""

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

        # --- AlertManager ---
        discord_cfg = get_nested(config, "discord", {})
        webhook_url = discord_cfg.get("webhook_url") or os.environ.get("DISCORD_WEBHOOK_URL")
        alert_enabled = discord_cfg.get("enabled", True)
        rate_limit = get_nested(config, "discord.cadence.out_of_hours", 60)
        self.alert_manager = AlertManager(
            webhook_url=webhook_url,
            enabled=alert_enabled,
            rate_limit_seconds=rate_limit,
        )

        # --- PaperTradeValidator ---
        self.paper_trade_validator = PaperTradeValidator(self.db_path)

        # --- MarketPhaseChecker ---
        schedule_cfg = get_nested(config, "schedule", {})
        risk_cfg = get_nested(config, "risk", {})
        holidays = schedule_cfg.get("holidays", [])
        self.market_phase = MarketPhaseChecker(
            holidays=holidays,
            entry_start=risk_cfg.get("entry_start_time", "09:20"),
            entry_end=risk_cfg.get("entry_end_time", "15:00"),
            force_exit_time=risk_cfg.get("force_exit_time", "15:15"),
            market_open=schedule_cfg.get("market_open", "09:15"),
            market_close=schedule_cfg.get("market_close", "15:30"),
        )

        # --- WatchlistManager ---
        self.watchlist_manager = WatchlistManager(config=config)

        # --- Wire callbacks ---
        self._setup_callbacks()

        # --- State ---
        self._daily_pnl_updated = False
        self._last_csv_export_date: str | None = None
        self._last_watchlist_reload: datetime | None = None

        logger.info(
            "trading_engine.initialized",
            db_path=self.db_path,
            paper_trade=paper_trade,
            strategy=self.strategy.name if self.strategy else "none",
            alerts_configured=self.alert_manager.is_configured,
            watchlist_symbols=len(self.watchlist_manager.symbols),
        )

    def _ensure_db_dir(self) -> None:
        """Ensure database directory exists."""
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    def _init_strategy(self, config: dict) -> 'BaseStrategy | None':
        """Initialize strategy from config."""
        strategy_config = get_nested(config, "strategies.st_rsi_trend_rider", {})
        if not strategy_config.get("enabled", False):
            logger.warning("trading_engine.no_strategy_enabled")
            return None

        if STRsiTrendRider is None:
            logger.error("trading_engine.alpha_engine_not_available")
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

        self.order_manager._fill_callbacks.append(self._on_order_fill)

    def _on_bar(self, bar: BarData) -> None:
        """Handle new bar from feed handler."""
        if self.strategy is None:
            return

        self._try_reload_watchlist()

        if self.market_phase.is_force_exit_time():
            self._force_exit_all("Market force-exit time reached")

        self.portfolio_manager.update_mtm(bar.symbol, bar.close)
        self.strategy.on_bar(bar)
        self._check_position_exits(bar)

    def _on_signal(self, signal: Signal) -> None:
        """Handle signal from strategy -> Risk validation -> Portfolio -> Orders."""
        logger.info(
            "trading_engine.signal_received",
            symbol=signal.symbol,
            type=signal.signal_type.value,
            price=signal.ltp,
        )

        asyncio.create_task(self.paper_trade_validator.log_signal(signal))

        if signal.signal_type in (SignalType.BUY, SignalType.SELL):
            asyncio.create_task(
                self.alert_manager.send_entry_alert(
                    symbol=signal.symbol,
                    price=signal.ltp,
                    stop_loss=signal.stop_loss,
                    target=signal.target,
                    quantity=signal.quantity,
                    reason=signal.reason,
                )
            )
        elif signal.signal_type in (SignalType.EXIT_LONG, SignalType.EXIT_SHORT):
            position = self.portfolio_manager.get_position(signal.symbol)
            pnl = position.realized_pnl if position else 0.0
            asyncio.create_task(
                self.alert_manager.send_exit_alert(
                    symbol=signal.symbol,
                    price=signal.ltp,
                    quantity=signal.quantity,
                    pnl=pnl,
                    reason=signal.reason,
                )
            )

        asyncio.create_task(self.signal_repo.save(signal))

        if signal.signal_type in (SignalType.BUY, SignalType.SELL):
            can_enter, reason = self.market_phase.can_enter_position()
            if not can_enter:
                logger.info(
                    "trading_engine.signal_blocked_by_phase",
                    symbol=signal.symbol,
                    reason=reason,
                )
                signal.rms_approved = False
                signal.rms_rejection_reason = reason
                return

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
        asyncio.create_task(self._process_portfolio_signal(signal))

    async def _process_portfolio_signal(self, signal: Signal) -> None:
        """Process signal through portfolio manager."""
        approved = await self.portfolio_manager.process_signal(signal)
        if not approved:
            return

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
            asyncio.create_task(
                self.alert_manager.send_sl_hit_alert(
                    symbol=position.symbol,
                    price=bar.close,
                    pnl=position.realized_pnl,
                )
            )
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

    def _force_exit_all(self, reason: str) -> None:
        """Force-exit all open positions."""
        positions = self.portfolio_manager.get_all_positions()
        for position in positions:
            if position.quantity != 0:
                logger.warning(
                    "trading_engine.force_exit",
                    symbol=position.symbol,
                    reason=reason,
                )
                self._emit_exit_signal(position, reason)

    def _try_reload_watchlist(self) -> None:
        """Hot-reload watchlist if config file changed."""
        try:
            if self.watchlist_manager.watchlist.reload_count == 0 or (
                self._last_watchlist_reload is not None
                and (datetime.now() - self._last_watchlist_reload).seconds > 30
            ):
                reloaded = self.watchlist_manager.reload_if_changed()
                if reloaded:
                    self._last_watchlist_reload = datetime.now()
        except Exception:
            logger.exception("trading_engine.watchlist_reload_error")

    async def initialize(self) -> None:
        """Initialize all components (DB, strategy warm-up)."""
        logger.info("trading_engine.initializing")

        migration_mgr = MigrationManager(self.db_path)
        await migration_mgr.migrate()

        await self.order_manager.initialize()

        total_capital = get_nested(self.config, "portfolio.allocation_pct", 50.0) * 100_000
        self.risk_manager.set_capital(total_capital)

        if self.strategy is not None and self.feed_handler is not None and self.feeder is not None:
            await self._warm_up()

        self._running = True
        logger.info("trading_engine.initialized")

    async def _warm_up(self) -> None:
        """Warm up strategy with historical data."""
        if self._warm_up_complete:
            return

        watchlist = self.watchlist_manager.symbols
        if not watchlist:
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
            try:
                while self._running:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
            return

        watchlist = self.watchlist_manager.symbols
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

        try:
            report = await self.paper_trade_validator.generate_daily_report()
            report_text = self.paper_trade_validator.format_report_text(report)
            logger.info("trading_engine.daily_report", report=report_text)

            if report.trades_count > 0:
                await self.alert_manager.send_daily_summary(
                    date=report.report_date,
                    total_pnl=report.total_pnl,
                    trades_count=report.trades_count,
                    win_rate=report.win_rate,
                    avg_pnl=report.avg_pnl,
                    max_drawdown=report.max_drawdown,
                )
        except Exception:
            logger.exception("trading_engine.daily_report_error")

        await self._export_trade_pivot()
        await self.alert_manager.close()

        if self.feed_handler is not None:
            try:
                await self.feed_handler.stop()
            except Exception:
                logger.exception("trading_engine.feed_stop_error")

        logger.info("trading_engine.shutdown_complete")

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
        market_status = self.market_phase.get_status()
        return {
            "running": self._running,
            "warm_up_complete": self._warm_up_complete,
            "strategy": self.strategy.name if self.strategy else None,
            "strategy_enabled": self.strategy.enabled if self.strategy else False,
            "risk_metrics": self.risk_manager.get_metrics(),
            "portfolio_metrics": self.portfolio_manager.get_metrics(),
            "order_metrics": self.order_manager.get_metrics(),
            "alert_metrics": self.alert_manager.get_metrics(),
            "market_phase": market_status.phase.value,
            "market_status": market_status.message,
            "can_enter": market_status.can_enter,
            "force_exit_active": market_status.force_exit_active,
            "watchlist_symbols": len(self.watchlist_manager.symbols),
            "watchlist_reloads": self.watchlist_manager.reload_count,
            "db_path": self.db_path,
        }

    def stop(self) -> None:
        """Signal the engine to stop."""
        self._running = False
