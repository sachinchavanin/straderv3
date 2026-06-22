"""Market data models — normalized tick and bar data structures."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TickData:
    """Normalized tick data from any broker feed.

    This is the canonical format consumed by strategies and portfolio manager.
    Broker-specific adapters must convert their payloads to this format.
    """

    symbol: str
    ltp: float
    timestamp: datetime
    volume: int = 0
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    close_price: float = 0.0
    change_pct: float = 0.0
    bid_price: float = 0.0
    ask_price: float = 0.0
    bid_qty: int = 0
    ask_qty: int = 0
    oi: int = 0

    @property
    def spread(self) -> float:
        if self.bid_price > 0 and self.ask_price > 0:
            return self.ask_price - self.bid_price
        return 0.0

    @property
    def has_depth(self) -> bool:
        return self.bid_qty > 0 and self.ask_qty > 0


@dataclass(slots=True)
class BarData:
    """OHLCV bar data for a specific timeframe."""

    symbol: str
    timestamp: datetime
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def range_size(self) -> float:
        return self.high - self.low


@dataclass(slots=True)
class MarketDepth:
    """Market depth (order book) data."""

    symbol: str
    timestamp: datetime
    bids: list[tuple[float, int]] = field(default_factory=list)
    asks: list[tuple[float, int]] = field(default_factory=list)

    @property
    def best_bid(self) -> tuple[float, int] | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> tuple[float, int] | None:
        return self.asks[0] if self.asks else None

    @property
    def total_bid_qty(self) -> int:
        return sum(qty for _, qty in self.bids)

    @property
    def total_ask_qty(self) -> int:
        return sum(qty for _, qty in self.asks)

    @property
    def has_liquidity(self) -> bool:
        return self.total_bid_qty > 0 and self.total_ask_qty > 0
