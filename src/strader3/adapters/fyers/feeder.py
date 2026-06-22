"""Fyers market data feeder implementation."""

import asyncio
from collections.abc import Callable
from datetime import datetime

import structlog

from strader3.adapters.base import DataFeeder
from strader3.adapters.fyers.auth import FyersAuth
from strader3.models import BarData, TickData

logger = structlog.get_logger(__name__)


class FyersFeeder(DataFeeder):
    """Fyers market data feeder.

    Implements the DataFeeder interface for Fyers API.
    Handles WebSocket connection, reconnection, and data normalization.

    Phase 1: WebSocket connection is scaffolded. Actual Fyers WebSocket
    integration requires valid credentials. Use delayed feed fallback for
    testing without live connection.
    """

    def __init__(
        self,
        auth: FyersAuth,
        reconnect_delay_initial: float = 1.0,
        reconnect_delay_max: float = 60.0,
        reconnect_delay_multiplier: float = 2.0,
        max_reconnect_attempts: int = 100,
    ):
        self._auth = auth
        self._reconnect_delay_initial = reconnect_delay_initial
        self._reconnect_delay_max = reconnect_delay_max
        self._reconnect_delay_multiplier = reconnect_delay_multiplier
        self._max_reconnect_attempts = max_reconnect_attempts

        self._connected = False
        self._subscribed_symbols: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

        # Callbacks
        self._tick_callbacks: list[Callable[[TickData], None]] = []
        self._bar_callbacks: list[Callable[[BarData], None]] = []
        self._disconnect_callbacks: list[Callable[[], None]] = []
        self._reconnect_callbacks: list[Callable[[], None]] = []

        # Reconnection state
        self._reconnect_attempts = 0
        self._reconnecting = False

        # LTP cache for quick access
        self._ltp_cache: dict[str, float] = {}

    @property
    def name(self) -> str:
        return "fyers"

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Connect to Fyers WebSocket.

        Phase 1: Validates authentication. Actual WebSocket connection
        requires fyers_apiv3.FyersWebsocket which needs valid credentials.
        """
        self._loop = asyncio.get_running_loop()
        if not self._auth.is_authenticated:
            success = await self._auth.authenticate()
            if not success:
                raise RuntimeError("Fyers authentication failed")

        logger.info(
            "Fyers WebSocket connection scaffolded (Phase 1)",
            authenticated=self._auth.is_connected if hasattr(self._auth, 'is_connected') else self._auth.is_authenticated,
        )
        # Phase 1: Mark as connected for testing purposes
        # In production, this would initialize the actual WebSocket
        self._connected = True

    async def disconnect(self) -> None:
        """Disconnect from WebSocket."""
        self._connected = False
        self._ws = None
        logger.info("Fyers WebSocket disconnected")

    async def subscribe(self, symbols: list[str]) -> None:
        """Subscribe to symbols."""
        self._subscribed_symbols.update(symbols)
        logger.info("Subscribed to symbols", symbols=symbols)

    async def unsubscribe(self, symbols: list[str]) -> None:
        """Unsubscribe from symbols."""
        self._subscribed_symbols -= set(symbols)
        logger.info("Unsubscribed from symbols", symbols=symbols)

    def on_tick(self, callback: Callable[[TickData], None]) -> None:
        self._tick_callbacks.append(callback)

    def on_bar(self, callback: Callable[[BarData], None]) -> None:
        self._bar_callbacks.append(callback)

    def on_disconnect(self, callback: Callable[[], None]) -> None:
        self._disconnect_callbacks.append(callback)

    def on_reconnect(self, callback: Callable[[], None]) -> None:
        self._reconnect_callbacks.append(callback)

    async def fetch_historical_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[BarData]:
        """Fetch historical bars for gap-filling.

        Phase 1: Returns empty list. Requires valid Fyers credentials
        and REST client initialization for actual data fetching.
        """
        logger.info(
            "fetch_historical_bars scaffolded (Phase 1)",
            symbol=symbol,
            timeframe=timeframe,
        )
        return []

    async def get_ltp(self, symbol: str) -> float:
        """Get last traded price."""
        if symbol in self._ltp_cache:
            return self._ltp_cache[symbol]
        return 0.0

    def inject_tick(self, tick: TickData) -> None:
        """Inject a tick directly (for testing / delayed feed fallback).

        This method allows feeding ticks without a live WebSocket connection,
        useful for backtesting replay or delayed data feeds.
        """
        self._ltp_cache[tick.symbol] = tick.ltp
        for callback in self._tick_callbacks:
            try:
                callback(tick)
            except Exception as e:
                logger.error("Tick callback error", error=str(e))
