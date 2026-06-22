"""Portfolio state models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class SymbolState(str, Enum):
    """Per-symbol state machine."""

    IDLE = "IDLE"
    PENDING_ENTRY = "PENDING_ENTRY"
    ACTIVE = "ACTIVE"
    PENDING_EXIT = "PENDING_EXIT"
    COOLDOWN = "COOLDOWN"


@dataclass(slots=True)
class CapitalSnapshot:
    """Point-in-time capital snapshot."""

    timestamp: datetime = field(default_factory=datetime.now)
    total_capital: float = 0.0
    available_margin: float = 0.0
    used_margin: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    @property
    def mtm_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def utilization_pct(self) -> float:
        if self.total_capital > 0:
            return (self.used_margin / self.total_capital) * 100
        return 0.0


@dataclass(slots=True)
class SectorExposure:
    """Sector exposure tracking."""

    sector: str
    symbols: list[str] = field(default_factory=list)
    total_value: float = 0.0
    position_count: int = 0

    def exposure_pct(self, total_capital: float) -> float:
        if total_capital > 0:
            return (self.total_value / total_capital) * 100
        return 0.0


@dataclass(slots=True)
class PortfolioState:
    """Complete portfolio state snapshot."""

    timestamp: datetime = field(default_factory=datetime.now)
    trading_enabled: bool = True
    entries_allowed: bool = True
    risk_mode: str = "NORMAL"
    capital: CapitalSnapshot = field(default_factory=CapitalSnapshot)
    active_positions: int = 0
    max_active_trades: int = 3
    symbol_states: dict[str, SymbolState] = field(default_factory=dict)
    sector_exposures: dict[str, SectorExposure] = field(default_factory=dict)
    trades_today: int = 0
    wins_today: int = 0
    losses_today: int = 0

    @property
    def can_enter_new_trade(self) -> bool:
        return (
            self.trading_enabled
            and self.entries_allowed
            and self.risk_mode == "NORMAL"
            and self.active_positions < self.max_active_trades
        )

    @property
    def win_rate_today(self) -> float:
        if self.trades_today > 0:
            return (self.wins_today / self.trades_today) * 100
        return 0.0
