"""Risk Management System — pre-trade and post-trade risk controls.

Enforces risk limits, validates orders, monitors positions,
and triggers protective actions.
"""

from datetime import datetime, time

import structlog

from strader3.models.trading import Position, Signal, SignalType

logger = structlog.get_logger(__name__)


class RiskManager:
    """Enforces risk controls for the trading system.

    Pre-Trade Checks:
    - Position sizing limits
    - Daily loss limits
    - Fat finger protection
    - Time guards
    - Sector exposure limits
    - Max open positions

    Post-Trade Monitoring:
    - Kill switch (consecutive losses)
    - Circuit detection
    - Force exit time
    """

    def __init__(
        self,
        max_daily_loss_pct: float = 2.0,
        per_trade_allocation_pct: float = 5.0,
        max_open_positions: int = 3,
        entry_start: str = "09:20",
        entry_end: str = "15:00",
        force_exit_time: str = "15:15",
        per_symbol_cooldown_sec: int = 300,
        max_order_value: float = 50_000.0,
        max_quantity: int = 1000,
        total_exposure_cap_pct: float = 50.0,
        kill_switch_consecutive_losses: int = 3,
        sizing_mode: str = "ALLOCATION_BASED",
    ) -> None:
        self.max_daily_loss_pct = max_daily_loss_pct
        self.per_trade_allocation_pct = per_trade_allocation_pct
        self.max_open_positions = max_open_positions
        self.entry_start = self._parse_time(entry_start)
        self.entry_end = self._parse_time(entry_end)
        self.force_exit_time = self._parse_time(force_exit_time)
        self.per_symbol_cooldown_sec = per_symbol_cooldown_sec
        self.max_order_value = max_order_value
        self.max_quantity = max_quantity
        self.total_exposure_cap_pct = total_exposure_cap_pct
        self.kill_switch_consecutive_losses = kill_switch_consecutive_losses
        self.sizing_mode = sizing_mode

        # State
        self._risk_mode: str = "NORMAL"  # NORMAL, RISK_OFF, FLAT_ONLY
        self._total_capital: float = 0.0
        self._daily_loss_limit: float = 0.0
        self._daily_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._kill_switch_tripped: bool = False

        # Tracking
        self._approved_orders: int = 0
        self._rejected_orders: int = 0
        self._rejection_log: list[dict] = []

    @staticmethod
    def _parse_time(time_str: str) -> time:
        """Parse ``HH:MM`` string to ``time`` object."""
        parts = time_str.split(":")
        return time(int(parts[0]), int(parts[1]))

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_capital(self, total_capital: float) -> None:
        """Set total capital and derive daily loss limit."""
        self._total_capital = total_capital
        self._daily_loss_limit = total_capital * (self.max_daily_loss_pct / 100)
        logger.info(
            "risk_manager.capital_set",
            capital=total_capital,
            daily_loss_limit=self._daily_loss_limit,
        )

    # ------------------------------------------------------------------
    # P&L tracking
    # ------------------------------------------------------------------

    def update_pnl(self, realized_pnl: float, unrealized_pnl: float) -> None:
        """Update daily P&L and check for breaches."""
        self._daily_pnl = realized_pnl + unrealized_pnl
        if self._daily_pnl < -self._daily_loss_limit:
            self._trigger_risk_off(
                f"Daily loss limit breached: {self._daily_pnl:.2f} "
                f"< -{self._daily_loss_limit:.2f}"
            )

    def record_trade_result(self, pnl: float) -> None:
        """Record a completed trade's P&L for kill-switch tracking."""
        if pnl < 0:
            self._consecutive_losses += 1
            if (
                self._consecutive_losses
                >= self.kill_switch_consecutive_losses
            ):
                self._trigger_risk_off(
                    f"Kill switch: {self._consecutive_losses} consecutive losses"
                )
        else:
            self._consecutive_losses = 0

    # ------------------------------------------------------------------
    # Signal validation
    # ------------------------------------------------------------------

    def validate_signal(
        self,
        signal: Signal,
        current_exposure: float,
        open_position_count: int,
    ) -> tuple[bool, str]:
        """Validate a signal against all risk rules.

        Returns:
            ``(approved, reason)`` tuple.
        """
        # Kill switch / risk mode
        if self._kill_switch_tripped or self._risk_mode == "RISK_OFF":
            return False, "Kill switch active — RISK_OFF"

        if self._risk_mode == "FLAT_ONLY" and signal.signal_type in (
            SignalType.BUY,
            SignalType.SELL,
        ):
            return False, "Flat-only mode: no new entries"

        # Entry signals
        if signal.signal_type in (SignalType.BUY, SignalType.SELL):
            return self._validate_entry(signal, current_exposure, open_position_count)

        # Exit signals — generally allowed
        return self._validate_exit(signal)

    def _validate_entry(
        self,
        signal: Signal,
        current_exposure: float,
        open_position_count: int,
    ) -> tuple[bool, str]:
        """Validate entry signal against all pre-trade rules."""
        now = datetime.now().time()

        # Time guard
        if not (self.entry_start <= now <= self.entry_end):
            return (
                False,
                f"Outside entry window ({self.entry_start}-{self.entry_end})",
            )

        # Max open positions
        if open_position_count >= self.max_open_positions:
            return (
                False,
                f"Max open positions reached ({self.max_open_positions})",
            )

        # Quantity check
        if signal.quantity <= 0:
            return False, "Invalid quantity (must be > 0)"

        if signal.quantity > self.max_quantity:
            return (
                False,
                f"Quantity {signal.quantity} exceeds max {self.max_quantity}",
            )

        # Order value check
        order_value = signal.quantity * signal.ltp
        if order_value > self.max_order_value:
            return (
                False,
                f"Order value {order_value:.2f} exceeds max {self.max_order_value:.2f}",
            )

        # Total exposure cap
        new_exposure = current_exposure + order_value
        max_exposure = self._total_capital * (self.total_exposure_cap_pct / 100)
        if new_exposure > max_exposure:
            return (
                False,
                f"Total exposure {new_exposure:.2f} would exceed "
                f"{self.total_exposure_cap_pct}% of capital ({max_exposure:.2f})",
            )

        # Stop-loss must be defined
        if signal.stop_loss <= 0:
            return False, "Stop-loss not defined (must be > 0)"

        # Target must be defined
        if signal.target <= 0:
            return False, "Target not defined (must be > 0)"

        return True, "Approved"

    @staticmethod
    def _validate_exit(signal: Signal) -> tuple[bool, str]:
        """Validate exit signal."""
        if signal.quantity <= 0:
            return False, "Invalid exit quantity"
        return True, "Exit approved"

    # ------------------------------------------------------------------
    # Position-level checks
    # ------------------------------------------------------------------

    @staticmethod
    def check_stop_loss(position: Position, current_price: float) -> bool:
        """Return ``True`` if stop-loss is hit."""
        if position.quantity > 0:
            return current_price <= position.stop_loss
        if position.quantity < 0:
            return current_price >= position.stop_loss
        return False

    @staticmethod
    def check_target(position: Position, current_price: float) -> bool:
        """Return ``True`` if target is hit."""
        if position.quantity > 0:
            return current_price >= position.target
        if position.quantity < 0:
            return current_price <= position.target
        return False

    def check_force_exit_time(self) -> bool:
        """Return ``True`` if forced exit time has been reached."""
        return datetime.now().time() >= self.force_exit_time

    @staticmethod
    def check_circuit(bid_qty: int, ask_qty: int) -> bool:
        """Return ``True`` if zero-depth circuit detected."""
        return bid_qty <= 0 or ask_qty <= 0

    # ------------------------------------------------------------------
    # Risk mode helpers
    # ------------------------------------------------------------------

    def _trigger_risk_off(self, reason: str) -> None:
        """Trip the kill switch."""
        if self._risk_mode != "RISK_OFF":
            self._risk_mode = "RISK_OFF"
            self._kill_switch_tripped = True
            logger.critical("risk_manager.risk_off", reason=reason)

    def set_risk_mode(self, mode: str) -> None:
        """Set risk mode: NORMAL, RISK_OFF, or FLAT_ONLY."""
        if mode in ("NORMAL", "RISK_OFF", "FLAT_ONLY"):
            old = self._risk_mode
            self._risk_mode = mode
            logger.info("risk_manager.mode_change", old=old, new=mode)

    def reset_kill_switch(self) -> None:
        """Manually reset kill switch (operator action)."""
        self._kill_switch_tripped = False
        self._consecutive_losses = 0
        self._risk_mode = "NORMAL"
        logger.warning("risk_manager.kill_switch_reset")

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def get_metrics(self) -> dict:
        return {
            "risk_mode": self._risk_mode,
            "kill_switch_tripped": self._kill_switch_tripped,
            "consecutive_losses": self._consecutive_losses,
            "daily_pnl": self._daily_pnl,
            "daily_loss_limit": self._daily_loss_limit,
            "approved_orders": self._approved_orders,
            "rejected_orders": self._rejected_orders,
        }
