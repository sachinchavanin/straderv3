"""Abstract base classes for broker-agnostic adapters.

These ABCs define the unified interface that all broker implementations must follow.
Core trading logic (strategies, RMS, portfolio manager) depends only on these interfaces.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable

from strader3.models import (
    BarData,
    Order,
    Position,
    TickData,
    Trade,
)


class DataFeeder(ABC):
    """Abstract base class for market data ingestion.

    Concrete implementations (FyersFeeder, etc.) must:
    1. Handle authentication with their specific broker
    2. Manage WebSocket connections with reconnection logic
    3. Normalize all data to TickData/BarData format
    4. Handle gap-filling after reconnection
    """

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @abstractmethod
    async def connect(self) -> None:
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        ...

    @abstractmethod
    async def subscribe(self, symbols: list[str]) -> None:
        ...

    @abstractmethod
    async def unsubscribe(self, symbols: list[str]) -> None:
        ...

    @abstractmethod
    def on_tick(self, callback: Callable[[TickData], None]) -> None:
        ...

    @abstractmethod
    def on_bar(self, callback: Callable[[BarData], None]) -> None:
        ...

    @abstractmethod
    def on_disconnect(self, callback: Callable[[], None]) -> None:
        ...

    @abstractmethod
    def on_reconnect(self, callback: Callable[[], None]) -> None:
        ...

    @abstractmethod
    async def fetch_historical_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[BarData]:
        ...

    @abstractmethod
    async def get_ltp(self, symbol: str) -> float:
        ...


class BrokerAdapter(ABC):
    """Abstract base class for order execution.

    Concrete implementations (FyersAdapter, etc.) must:
    1. Handle authentication with their specific broker
    2. Normalize order requests/responses to common format
    3. Handle rate limiting per broker specs
    4. Support sandbox/paper trading mode
    """

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def is_authenticated(self) -> bool:
        ...

    @property
    @abstractmethod
    def is_sandbox(self) -> bool:
        ...

    @abstractmethod
    async def authenticate(self) -> bool:
        ...

    @abstractmethod
    async def place_order(self, order: Order) -> Order:
        ...

    @abstractmethod
    async def modify_order(self, order: Order) -> Order:
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> Order | None:
        ...

    @abstractmethod
    async def get_orders(self) -> list[Order]:
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        ...

    @abstractmethod
    async def get_trades(self) -> list[Trade]:
        ...

    @abstractmethod
    async def get_funds(self) -> dict:
        ...

    @abstractmethod
    async def square_off_all(self) -> list[Order]:
        ...
