"""Trading models — signals, orders, positions, trades."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import uuid4


class SignalType(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    EXIT_LONG = "EXIT_LONG"
    EXIT_SHORT = "EXIT_SHORT"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_MARKET = "SL_MARKET"


class ProductType(StrEnum):
    MIS = "MIS"
    CNC = "CNC"
    NRML = "NRML"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    ERROR = "ERROR"


class PositionState(StrEnum):
    IDLE = "IDLE"
    PENDING_ENTRY = "PENDING_ENTRY"
    ACTIVE = "ACTIVE"
    PENDING_EXIT = "PENDING_EXIT"
    COOLDOWN = "COOLDOWN"


@dataclass(slots=True)
class Signal:
    """Trading signal emitted by strategy.

    Broker-agnostic intent object that flows through
    Portfolio Manager -> RMS -> OMS.
    """

    id: str = field(default_factory=lambda: str(uuid4()))
    symbol: str = ""
    signal_type: SignalType = SignalType.BUY
    strategy_name: str = ""
    ltp: float = 0.0
    stop_loss: float = 0.0
    target: float = 0.0
    max_holding_minutes: int = 180
    timestamp: datetime = field(default_factory=datetime.now)
    reason: str = ""
    indicators: dict = field(default_factory=dict)
    quantity: int = 0
    allocated_capital: float = 0.0
    rms_approved: bool = False
    rms_rejection_reason: str = ""


@dataclass(slots=True)
class Order:
    """Order object for OMS."""

    id: str = field(default_factory=lambda: str(uuid4()))
    client_order_id: str = ""
    symbol: str = ""
    side: SignalType = SignalType.BUY
    order_type: OrderType = OrderType.MARKET
    product_type: ProductType = ProductType.MIS
    quantity: int = 0
    filled_quantity: int = 0
    price: float = 0.0
    trigger_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    average_price: float = 0.0
    created_at: datetime = field(default_factory=datetime.now)
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    cancelled_at: datetime | None = None
    signal_id: str = ""
    parent_order_id: str = ""
    rejection_reason: str = ""
    broker_message: str = ""

    @property
    def is_complete(self) -> bool:
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        )

    @property
    def pending_quantity(self) -> int:
        return self.quantity - self.filled_quantity


@dataclass(slots=True)
class Position:
    """Open position."""

    symbol: str
    state: PositionState = PositionState.IDLE
    quantity: int = 0
    average_price: float = 0.0
    current_price: float = 0.0
    entry_order_id: str = ""
    entry_time: datetime | None = None
    entry_signal_id: str = ""
    strategy_name: str = ""
    stop_loss: float = 0.0
    target: float = 0.0
    trailing_stop: float = 0.0
    max_holding_until: datetime | None = None
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    sector: str = ""

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def market_value(self) -> float:
        return abs(self.quantity) * self.current_price

    def update_mtm(self, ltp: float) -> None:
        self.current_price = ltp
        if self.quantity != 0:
            self.unrealized_pnl = (ltp - self.average_price) * self.quantity


@dataclass(slots=True)
class Trade:
    """Completed trade record for persistence."""

    id: str = field(default_factory=lambda: str(uuid4()))
    symbol: str = ""
    strategy_name: str = ""
    entry_time: datetime | None = None
    entry_price: float = 0.0
    entry_quantity: int = 0
    entry_order_id: str = ""
    exit_time: datetime | None = None
    exit_price: float = 0.0
    exit_quantity: int = 0
    exit_order_id: str = ""
    exit_reason: str = ""
    gross_pnl: float = 0.0
    charges: float = 0.0
    net_pnl: float = 0.0
    signal_price: float = 0.0
    entry_slippage: float = 0.0
    exit_slippage: float = 0.0
    execution_delay_ms: int = 0
    signal_id: str = ""
    sector: str = ""
