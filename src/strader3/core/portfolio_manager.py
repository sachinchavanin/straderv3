"""Portfolio Manager — multi-symbol orchestration and capital allocation.

Manages per-symbol state machines, allocates capital, and tracks positions.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta

import structlog

from strader3.models.portfolio import (
    CapitalSnapshot,
    PortfolioState,
    SectorExposure,
    SymbolState,
)
from strader3.models.trading import (
    Position,
    PositionState,
    Signal,
    SignalType,
)

logger = structlog.get_logger(__name__)


class InvalidTransitionError(Exception):
    """Raised when an invalid state machine transition is attempted."""

    def __init__(self, symbol: str, current: SymbolState, attempted: str) -> None:
        super().__init__(
            f"Invalid transition for {symbol}: {current.value} -> {attempted}"
        )
        self.symbol = symbol
        self.current = current
        self.attempted = attempted


class CapitalAllocator:
    """Manages capital allocation for trades."""

    def __init__(
        self,
        total_capital: float,
        allocation_pct: float = 5.0,
        max_active_trades: int = 3,
        max_sector_exposure_pct: float = 30.0,
        sizing_mode: str = "ALLOCATION_BASED",
    ) -> None:
        self.total_capital = total_capital
        self.allocation_pct = allocation_pct
        self.max_active_trades = max_active_trades
        self.max_sector_exposure_pct = max_sector_exposure_pct
        self.sizing_mode = sizing_mode

        self._used_capital: float = 0.0
        self._active_trades: int = 0
        self._sector_exposure: dict[str, float] = {}

    @property
    def available_capital(self) -> float:
        return self.total_capital - self._used_capital

    @property
    def can_allocate(self) -> bool:
        return self._active_trades < self.max_active_trades

    def calculate_quantity(self, ltp: float, stop_loss: float = 0.0) -> int:
        """Calculate order quantity based on allocation rules.

        Returns:
            Quantity to trade, or 0 if allocation not possible.
        """
        if not self.can_allocate or ltp <= 0:
            return 0

        if self.sizing_mode == "RISK_BASED" and stop_loss > 0:
            risk_amount = self.total_capital * 0.005  # 0.5% risk per trade
            risk_per_share = abs(ltp - stop_loss)
            if risk_per_share <= 0:
                return 0
            quantity = int(risk_amount / risk_per_share)
            max_qty = int(self.available_capital / ltp)
            quantity = min(quantity, max_qty)
        else:
            allocation = self.total_capital * (self.allocation_pct / 100)
            if allocation > self.available_capital:
                allocation = self.available_capital
            quantity = int(allocation / ltp)

        return max(quantity, 0)

    def check_sector_limit(self, sector: str, additional_value: float) -> bool:
        """Return ``True`` if sector exposure allows the trade."""
        current = self._sector_exposure.get(sector, 0)
        new_total = current + additional_value
        max_allowed = self.total_capital * (self.max_sector_exposure_pct / 100)
        return new_total <= max_allowed

    def allocate(self, symbol: str, value: float, sector: str = "") -> bool:
        """Allocate capital for a trade.

        Returns:
            True if allocation succeeded.
        """
        if not self.can_allocate:
            return False
        if value > self.available_capital:
            return False
        if sector and not self.check_sector_limit(sector, value):
            return False

        self._used_capital += value
        self._active_trades += 1
        if sector:
            self._sector_exposure[sector] = (
                self._sector_exposure.get(sector, 0) + value
            )
        logger.info(
            "allocator.allocated",
            symbol=symbol,
            value=value,
            sector=sector,
            active_trades=self._active_trades,
        )
        return True

    def release(self, symbol: str, value: float, sector: str = "") -> None:
        """Release allocated capital after trade exit."""
        self._used_capital = max(0.0, self._used_capital - value)
        self._active_trades = max(0, self._active_trades - 1)
        if sector and sector in self._sector_exposure:
            self._sector_exposure[sector] = max(
                0.0, self._sector_exposure[sector] - value
            )
        logger.info(
            "allocator.released",
            symbol=symbol,
            value=value,
            active_trades=self._active_trades,
        )

    def get_snapshot(self) -> CapitalSnapshot:
        return CapitalSnapshot(
            total_capital=self.total_capital,
            available_margin=self.available_capital,
            used_margin=self._used_capital,
        )

    def get_sector_exposures(self) -> dict[str, SectorExposure]:
        return {
            sector: SectorExposure(
                sector=sector,
                total_value=value,
                position_count=1,
            )
            for sector, value in self._sector_exposure.items()
        }


class PortfolioManager:
    """Orchestrates multi-symbol trading with per-symbol state machines.

    State machine per symbol:
        IDLE -> PENDING_ENTRY -> ACTIVE -> PENDING_EXIT -> COOLDOWN -> IDLE
    """

    # Valid transitions: current_state -> {allowed_next_states}
    VALID_TRANSITIONS: dict[SymbolState, set[SymbolState]] = {
        SymbolState.IDLE: {SymbolState.PENDING_ENTRY},
        SymbolState.PENDING_ENTRY: {SymbolState.ACTIVE, SymbolState.IDLE},
        SymbolState.ACTIVE: {SymbolState.PENDING_EXIT},
        SymbolState.PENDING_EXIT: {SymbolState.COOLDOWN, SymbolState.ACTIVE},
        SymbolState.COOLDOWN: {SymbolState.IDLE},
    }

    def __init__(
        self,
        capital_allocator: CapitalAllocator,
        sector_map: dict[str, list[str]] | None = None,
        cooldown_seconds: int = 300,
    ) -> None:
        self.allocator = capital_allocator
        self.sector_map = sector_map or {}
        self.cooldown_seconds = cooldown_seconds

        # Reverse lookup: symbol -> sector
        self._symbol_to_sector: dict[str, str] = {}
        for sector, symbols in self.sector_map.items():
            for sym in symbols:
                self._symbol_to_sector[sym] = sector

        # Per-symbol state
        self._symbol_states: dict[str, SymbolState] = {}
        self._symbol_locks: dict[str, asyncio.Lock] = {}
        self._positions: dict[str, Position] = {}
        self._cooldown_until: dict[str, datetime] = {}

        # Callbacks
        self._position_update_callbacks: list[Callable[[Position], None]] = []

        # Metrics
        self._signals_received: int = 0
        self._signals_approved: int = 0
        self._signals_rejected: int = 0

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def get_symbol_state(self, symbol: str) -> SymbolState:
        return self._symbol_states.get(symbol, SymbolState.IDLE)

    def transition(self, symbol: str, new_state: SymbolState) -> None:
        """Transition a symbol to a new state.

        Raises:
            InvalidTransitionError: If the transition is not allowed.
        """
        current = self.get_symbol_state(symbol)
        allowed = self.VALID_TRANSITIONS.get(current, set())
        if new_state not in allowed:
            raise InvalidTransitionError(symbol, current, new_state.value)
        self._symbol_states[symbol] = new_state
        logger.debug(
            "portfolio.state_transition",
            symbol=symbol,
            old=current.value,
            new=new_state.value,
        )

    # ------------------------------------------------------------------
    # Signal processing
    # ------------------------------------------------------------------

    def _get_lock(self, symbol: str) -> asyncio.Lock:
        if symbol not in self._symbol_locks:
            self._symbol_locks[symbol] = asyncio.Lock()
        return self._symbol_locks[symbol]

    async def process_signal(self, signal: Signal) -> bool:
        """Process incoming signal from strategy.

        Returns:
            True if signal was approved and forwarded to RMS.
        """
        self._signals_received += 1
        symbol = signal.symbol

        async with self._get_lock(symbol):
            state = self.get_symbol_state(symbol)

            if signal.signal_type in (SignalType.BUY, SignalType.SELL):
                return await self._handle_entry_signal(signal, state)
            if signal.signal_type in (
                SignalType.EXIT_LONG,
                SignalType.EXIT_SHORT,
            ):
                return self._handle_exit_signal(signal, state)

            logger.warning("portfolio.unknown_signal_type", type=signal.signal_type)
            self._signals_rejected += 1
            return False

    async def _handle_entry_signal(self, signal: Signal, state: SymbolState) -> bool:
        """Handle BUY/SELL entry signals."""
        if state != SymbolState.IDLE:
            logger.debug(
                "portfolio.entry_rejected_not_idle",
                symbol=signal.symbol,
                state=state.value,
            )
            self._signals_rejected += 1
            return False

        # Cooldown check
        if signal.symbol in self._cooldown_until:
            if datetime.now() < self._cooldown_until[signal.symbol]:
                logger.debug(
                    "portfolio.entry_rejected_cooldown",
                    symbol=signal.symbol,
                )
                self._signals_rejected += 1
                return False

        # Calculate quantity
        quantity = self.allocator.calculate_quantity(
            signal.ltp, stop_loss=signal.stop_loss
        )
        if quantity <= 0:
            logger.debug(
                "portfolio.entry_rejected_no_quantity",
                symbol=signal.symbol,
            )
            self._signals_rejected += 1
            return False

        # Sector limit check
        sector = self._symbol_to_sector.get(signal.symbol, "")
        trade_value = quantity * signal.ltp
        if sector and not self.allocator.check_sector_limit(sector, trade_value):
            logger.debug(
                "portfolio.entry_rejected_sector_limit",
                symbol=signal.symbol,
                sector=sector,
            )
            self._signals_rejected += 1
            return False

        # Allocate capital
        if not self.allocator.allocate(signal.symbol, trade_value, sector):
            logger.debug(
                "portfolio.entry_rejected_allocation",
                symbol=signal.symbol,
            )
            self._signals_rejected += 1
            return False

        # Update signal
        signal.quantity = quantity
        signal.allocated_capital = trade_value

        # State transition: IDLE -> PENDING_ENTRY
        self.transition(signal.symbol, SymbolState.PENDING_ENTRY)

        # Create position
        self._positions[signal.symbol] = Position(
            symbol=signal.symbol,
            state=PositionState.PENDING_ENTRY,
            entry_signal_id=signal.id,
            strategy_name=signal.strategy_name,
            stop_loss=signal.stop_loss,
            target=signal.target,
            sector=sector,
            max_holding_until=datetime.now()
            + timedelta(minutes=signal.max_holding_minutes),
        )

        self._signals_approved += 1
        logger.info(
            "portfolio.entry_approved",
            symbol=signal.symbol,
            type=signal.signal_type.value,
            quantity=quantity,
        )
        return True

    def _handle_exit_signal(self, signal: Signal, state: SymbolState) -> bool:
        """Handle EXIT_LONG/EXIT_SHORT signals."""
        if state != SymbolState.ACTIVE:
            logger.debug(
                "portfolio.exit_rejected_not_active",
                symbol=signal.symbol,
                state=state.value,
            )
            self._signals_rejected += 1
            return False

        position = self._positions.get(signal.symbol)
        if position:
            signal.quantity = abs(position.quantity)

        # State transition: ACTIVE -> PENDING_EXIT
        self.transition(signal.symbol, SymbolState.PENDING_EXIT)

        self._signals_approved += 1
        logger.info(
            "portfolio.exit_approved",
            symbol=signal.symbol,
            type=signal.signal_type.value,
        )
        return True

    # ------------------------------------------------------------------
    # Order fill handling
    # ------------------------------------------------------------------

    def on_order_filled(
        self,
        symbol: str,
        quantity: int,
        price: float,
        is_entry: bool,
    ) -> None:
        """Handle order fill notification from OMS."""
        position = self._positions.get(symbol)
        if not position:
            logger.warning("portfolio.fill_no_position", symbol=symbol)
            return

        if is_entry:
            position.state = PositionState.ACTIVE
            position.quantity = quantity if position.state != PositionState.IDLE else quantity
            position.average_price = price
            position.entry_time = datetime.now()
            self.transition(symbol, SymbolState.ACTIVE)
            logger.info(
                "portfolio.position_opened",
                symbol=symbol,
                quantity=quantity,
                price=price,
            )
        else:
            # Exit fill
            sector = position.sector
            trade_value = abs(position.quantity) * position.average_price
            self.allocator.release(symbol, trade_value, sector)

            # State transition: PENDING_EXIT -> COOLDOWN
            self.transition(symbol, SymbolState.COOLDOWN)
            self._cooldown_until[symbol] = datetime.now() + timedelta(
                seconds=self.cooldown_seconds
            )

            # Clear position
            del self._positions[symbol]
            logger.info("portfolio.position_closed", symbol=symbol)

        # Notify callbacks
        for cb in self._position_update_callbacks:
            try:
                cb(position)
            except Exception:
                logger.exception("portfolio.position_callback_error")

    # ------------------------------------------------------------------
    # Position queries
    # ------------------------------------------------------------------

    def get_position(self, symbol: str) -> Position | None:
        return self._positions.get(symbol)

    def get_all_positions(self) -> list[Position]:
        return list(self._positions.values())

    def update_mtm(self, symbol: str, ltp: float) -> None:
        position = self._positions.get(symbol)
        if position and position.quantity != 0:
            position.update_mtm(ltp)

    def get_portfolio_state(self) -> PortfolioState:
        return PortfolioState(
            trading_enabled=True,
            entries_allowed=self.allocator.can_allocate,
            capital=self.allocator.get_snapshot(),
            active_positions=len(self._positions),
            max_active_trades=self.allocator.max_active_trades,
            symbol_states=dict(self._symbol_states),
            sector_exposures=self.allocator.get_sector_exposures(),
        )

    def force_exit_all(self) -> list[Signal]:
        """Generate exit signals for all positions (square-off)."""
        exit_signals: list[Signal] = []
        for symbol, position in self._positions.items():
            if position.quantity != 0:
                sig_type = (
                    SignalType.EXIT_LONG
                    if position.quantity > 0
                    else SignalType.EXIT_SHORT
                )
                exit_signals.append(
                    Signal(
                        symbol=symbol,
                        signal_type=sig_type,
                        ltp=position.current_price,
                        quantity=abs(position.quantity),
                        reason="Force exit — square off all",
                    )
                )
        return exit_signals

    def force_exit_symbol(self, symbol: str) -> Signal | None:
        """Generate exit signal for a single symbol."""
        position = self._positions.get(symbol)
        if position is None or position.quantity == 0:
            return None
        sig_type = (
            SignalType.EXIT_LONG
            if position.quantity > 0
            else SignalType.EXIT_SHORT
        )
        return Signal(
            symbol=symbol,
            signal_type=sig_type,
            ltp=position.current_price,
            quantity=abs(position.quantity),
            reason=f"Position exit — {symbol}",
        )

    # ------------------------------------------------------------------
    # Cooldown management
    # ------------------------------------------------------------------

    def tick_cooldown(self) -> list[str]:
        """Expire cooldowns that have elapsed. Returns list of symbols expired."""
        now = datetime.now()
        expired = [
            sym
            for sym, until in self._cooldown_until.items()
            if now >= until
        ]
        for sym in expired:
            del self._cooldown_until[sym]
            self.transition(sym, SymbolState.IDLE)
        return expired

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def get_metrics(self) -> dict:
        return {
            "signals_received": self._signals_received,
            "signals_approved": self._signals_approved,
            "signals_rejected": self._signals_rejected,
            "active_positions": len(self._positions),
            "capital_used": self.allocator._used_capital,
            "capital_available": self.allocator.available_capital,
        }
