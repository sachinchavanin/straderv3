"""Feed Handler — Market data ingestion and distribution.

This module orchestrates data ingestion from broker feeders,
manages in-memory caching (no Redis), and distributes normalized
data to consumers.
"""

import asyncio
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime, timedelta

import structlog

from strader3.adapters.base import DataFeeder
from strader3.models import BarData, TickData

logger = structlog.get_logger(__name__)


class BarAggregator:
    """Aggregates ticks into time-based bars."""

    def __init__(self, timeframe: str = "1m"):
        self.timeframe = timeframe
        self._current_bars: dict[str, dict] = {}
        self._bar_callbacks: list[Callable[[BarData], None]] = []
        self._interval_seconds = self._parse_timeframe(timeframe)

    def _parse_timeframe(self, tf: str) -> int:
        if tf.endswith("m"):
            return int(tf[:-1]) * 60
        elif tf.endswith("h"):
            return int(tf[:-1]) * 3600
        elif tf.endswith("d"):
            return int(tf[:-1]) * 86400
        return 60

    def on_bar(self, callback: Callable[[BarData], None]) -> None:
        self._bar_callbacks.append(callback)

    def process_tick(self, tick: TickData) -> BarData | None:
        """Process tick and emit bar if interval complete."""
        symbol = tick.symbol
        bar_start = self._get_bar_start(tick.timestamp)

        if symbol not in self._current_bars:
            self._current_bars[symbol] = {
                "start": bar_start,
                "open": tick.ltp,
                "high": tick.ltp,
                "low": tick.ltp,
                "close": tick.ltp,
                "volume": tick.volume,
            }
            return None

        current = self._current_bars[symbol]

        if bar_start > current["start"]:
            completed_bar = BarData(
                symbol=symbol,
                timestamp=current["start"],
                timeframe=self.timeframe,
                open=current["open"],
                high=current["high"],
                low=current["low"],
                close=current["close"],
                volume=current["volume"],
            )

            self._current_bars[symbol] = {
                "start": bar_start,
                "open": tick.ltp,
                "high": tick.ltp,
                "low": tick.ltp,
                "close": tick.ltp,
                "volume": tick.volume,
            }

            for callback in self._bar_callbacks:
                try:
                    callback(completed_bar)
                except Exception as e:
                    logger.error("Bar callback error", error=str(e))

            return completed_bar

        current["high"] = max(current["high"], tick.ltp)
        current["low"] = min(current["low"], tick.ltp)
        current["close"] = tick.ltp
        current["volume"] = tick.volume

        return None

    def _get_bar_start(self, timestamp: datetime) -> datetime:
        seconds = int(timestamp.timestamp())
        bar_start = seconds - (seconds % self._interval_seconds)
        return datetime.fromtimestamp(bar_start)


class FeedHandler:
    """Orchestrates market data ingestion.

    Responsibilities:
    - Manages DataFeeder connections
    - Caches LTP in-memory (no Redis)
    - Aggregates ticks into bars
    - Handles gap-filling after reconnection
    - Distributes data to consumers (strategies, portfolio manager)
    """

    def __init__(
        self,
        feeder: DataFeeder,
        bar_interval: str = "1m",
        gap_fill_minutes: int = 15,
    ):
        self.feeder = feeder
        self.gap_fill_minutes = gap_fill_minutes

        self._bar_aggregator = BarAggregator(bar_interval)
        self._running = False
        self._symbols: list[str] = []

        # Callbacks
        self._tick_callbacks: list[Callable[[TickData], None]] = []
        self._bar_callbacks: list[Callable[[BarData], None]] = []

        # In-memory cache (replaces Redis)
        self._ltp_cache: dict[str, TickData] = {}
        self._bar_cache: dict[str, deque[BarData]] = defaultdict(
            lambda: deque(maxlen=200)
        )

        # Metrics
        self._tick_count = 0
        self._last_tick_time: datetime | None = None
        self._reconnect_count = 0

        # Gap fill state
        self._gap_fill_pending = False
        self._last_disconnect_time: datetime | None = None

        # Wire up feeder callbacks
        self.feeder.on_tick(self._on_tick)
        self.feeder.on_disconnect(self._on_disconnect)
        self.feeder.on_reconnect(self._on_reconnect)
        self._bar_aggregator.on_bar(self._on_bar)

    @property
    def is_connected(self) -> bool:
        return self.feeder.is_connected

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    def on_tick(self, callback: Callable[[TickData], None]) -> None:
        self._tick_callbacks.append(callback)

    def on_bar(self, callback: Callable[[BarData], None]) -> None:
        self._bar_callbacks.append(callback)

    async def start(self, symbols: list[str]) -> None:
        """Start the feed handler."""
        self._symbols = symbols
        self._running = True

        logger.info("Starting feed handler", symbols_count=len(symbols))

        await self.feeder.connect()
        await self.feeder.subscribe(symbols)

        logger.info("Feed handler started")

    async def stop(self) -> None:
        """Stop the feed handler."""
        self._running = False
        await self.feeder.disconnect()
        logger.info("Feed handler stopped", total_ticks=self._tick_count)

    def _on_tick(self, tick: TickData) -> None:
        """Handle incoming tick from feeder."""
        if not self._running:
            return

        self._tick_count += 1
        self._last_tick_time = tick.timestamp

        # Cache LTP in-memory (replaces Redis)
        self._ltp_cache[tick.symbol] = tick

        # Aggregate into bars
        self._bar_aggregator.process_tick(tick)

        # Notify tick callbacks
        for callback in self._tick_callbacks:
            try:
                callback(tick)
            except Exception as e:
                logger.error("Tick callback error in feed handler", error=str(e))

    def _on_bar(self, bar: BarData) -> None:
        """Handle completed bar from aggregator."""
        # Cache bar in-memory (replaces Redis)
        self._bar_cache[bar.symbol].append(bar)

        # Notify bar callbacks
        for callback in self._bar_callbacks:
            try:
                callback(bar)
            except Exception as e:
                logger.error("Bar callback error", error=str(e))

    def _on_disconnect(self) -> None:
        """Handle feeder disconnect."""
        self._last_disconnect_time = datetime.now()
        self._gap_fill_pending = True
        logger.warning("Feed disconnected, gap fill pending")

    def _on_reconnect(self) -> None:
        """Handle feeder reconnect."""
        self._reconnect_count += 1

        if self._gap_fill_pending:
            asyncio.create_task(self._perform_gap_fill())

    async def _perform_gap_fill(self) -> None:
        """Fetch historical data to fill gaps after reconnect."""
        if not self._last_disconnect_time:
            self._gap_fill_pending = False
            return

        logger.info("Starting gap fill")

        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=self.gap_fill_minutes)

        filled_bars = 0

        for symbol in self._symbols:
            try:
                bars = await self.feeder.fetch_historical_bars(
                    symbol=symbol,
                    timeframe="1m",
                    start=start_time,
                    end=end_time,
                )

                for bar in bars:
                    self._bar_cache[bar.symbol].append(bar)
                    filled_bars += 1

            except Exception as e:
                logger.error("Gap fill error", symbol=symbol, error=str(e))

        self._gap_fill_pending = False
        logger.info("Gap fill complete", bars_filled=filled_bars)

    def get_ltp(self, symbol: str) -> float:
        """Get cached LTP for symbol (in-memory, no Redis)."""
        tick = self._ltp_cache.get(symbol)
        if tick:
            return tick.ltp
        return 0.0

    def get_recent_bars(self, symbol: str, count: int = 100) -> list[BarData]:
        """Get recent cached bars for symbol (in-memory, no Redis)."""
        bars = self._bar_cache.get(symbol, deque())
        return list(bars)[-count:]

    def get_metrics(self) -> dict:
        """Get feed handler metrics."""
        return {
            "connected": self.is_connected,
            "tick_count": self._tick_count,
            "reconnect_count": self._reconnect_count,
            "last_tick_time": (
                self._last_tick_time.isoformat() if self._last_tick_time else None
            ),
            "symbols_count": len(self._symbols),
            "gap_fill_pending": self._gap_fill_pending,
        }
