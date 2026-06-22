"""Alpha Engine — Strategy management and signal generation.

This module defines the strategy interface and manages strategy execution.
Strategies emit broker-agnostic signals that flow to Portfolio Manager.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd
import pandas_ta as ta
import structlog

from strader3.models import BarData, Signal, SignalType, TickData

if TYPE_CHECKING:
    from strader3.adapters.base import DataFeeder

logger = structlog.get_logger(__name__)


class BaseStrategy(ABC):
    """Abstract base class for trading strategies.

    Strategies must:
    - Implement on_tick and/or on_bar handlers
    - Emit Signal objects (not raw orders)
    - Include stop_loss, target, max_holding_time in every signal
    - Remain broker-agnostic (no broker SDK imports)
    """

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self._enabled = config.get("enabled", False)
        self._signal_callbacks: list[Callable[[Signal], None]] = []

        # State per symbol
        self._symbol_data: dict[str, pd.DataFrame] = {}
        self._last_signal_time: dict[str, datetime] = {}
        self._warmed_up_symbols: set[str] = set()
        self._last_historical_timestamp: dict[str, datetime] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        self._enabled = True
        logger.info("Strategy enabled", name=self.name)

    def disable(self) -> None:
        self._enabled = False
        logger.info("Strategy disabled", name=self.name)

    def on_signal(self, callback: Callable[[Signal], None]) -> None:
        self._signal_callbacks.append(callback)

    def emit_signal(self, signal: Signal) -> None:
        signal.strategy_name = self.name

        for callback in self._signal_callbacks:
            try:
                callback(signal)
            except Exception as e:
                logger.error("Signal callback error", error=str(e))

        self._last_signal_time[signal.symbol] = signal.timestamp
        logger.info(
            "Signal emitted",
            strategy=self.name,
            symbol=signal.symbol,
            type=signal.signal_type.value,
        )

    def update_bar_data(self, symbol: str, bar: BarData) -> None:
        """Update internal bar data for a symbol."""
        if symbol not in self._symbol_data:
            self._symbol_data[symbol] = pd.DataFrame(
                columns=["timestamp", "open", "high", "low", "close", "volume"]
            )

        existing_df = self._symbol_data[symbol]
        if not existing_df.empty and bar.timestamp in existing_df["timestamp"].values:
            return

        new_row = pd.DataFrame(
            [
                {
                    "timestamp": bar.timestamp,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
            ]
        )

        self._symbol_data[symbol] = pd.concat(
            [self._symbol_data[symbol], new_row], ignore_index=True
        ).tail(200)

    def get_bars(self, symbol: str) -> pd.DataFrame:
        return self._symbol_data.get(symbol, pd.DataFrame())

    async def warm_up(
        self,
        symbols: list[str],
        data_feeder: "DataFeeder",
        lookback_bars: int = 100,
        timeframe: str = "1m",
    ) -> None:
        """Warm up strategy with historical bars."""
        for symbol in symbols:
            try:
                end_time = datetime.now()
                timeframe_minutes = self._parse_timeframe_minutes(timeframe)
                start_time = end_time - timedelta(
                    minutes=lookback_bars * timeframe_minutes
                )

                logger.info(
                    "Starting warm-up for symbol",
                    strategy=self.name,
                    symbol=symbol,
                    lookback_bars=lookback_bars,
                )

                historical_bars = await data_feeder.fetch_historical_bars(
                    symbol=symbol,
                    timeframe=timeframe,
                    start=start_time,
                    end=end_time,
                )

                if not historical_bars:
                    logger.warning(
                        "No historical bars fetched for warm-up",
                        strategy=self.name,
                        symbol=symbol,
                    )
                    continue

                for bar in historical_bars:
                    self.update_bar_data(symbol, bar)

                if historical_bars:
                    self._last_historical_timestamp[symbol] = historical_bars[
                        -1
                    ].timestamp

                self._warmed_up_symbols.add(symbol)

                logger.info(
                    "Warm-up completed for symbol",
                    strategy=self.name,
                    symbol=symbol,
                    bars_loaded=len(historical_bars),
                )

            except Exception as e:
                logger.error(
                    "Warm-up failed for symbol",
                    strategy=self.name,
                    symbol=symbol,
                    error=str(e),
                )

    def _parse_timeframe_minutes(self, timeframe: str) -> int:
        if timeframe.endswith("m"):
            return int(timeframe[:-1])
        elif timeframe.endswith("h"):
            return int(timeframe[:-1]) * 60
        elif timeframe.endswith("d"):
            return int(timeframe[:-1]) * 60 * 24
        return 1

    def is_warmed_up(self, symbol: str) -> bool:
        return symbol in self._warmed_up_symbols

    @abstractmethod
    def on_tick(self, tick: TickData) -> None:
        pass

    @abstractmethod
    def on_bar(self, bar: BarData) -> None:
        pass

    @abstractmethod
    def calculate_stop_loss(
        self, symbol: str, entry_price: float, side: SignalType
    ) -> float:
        pass

    @abstractmethod
    def calculate_target(
        self, symbol: str, entry_price: float, side: SignalType
    ) -> float:
        pass


class STRsiTrendRider(BaseStrategy):
    """Supertrend + RSI Trend Riding Strategy.

    Entry Logic:
    - Long: Supertrend bullish + RSI crosses above oversold
    - Short: Supertrend bearish + RSI crosses below overbought

    Exit Logic:
    - Stop-loss at ATR multiple
    - Target at ATR multiple
    - Time-based exit after max holding period
    """

    def __init__(self, config: dict):
        super().__init__("st_rsi_trend_rider", config)

        # Strategy parameters
        self.st_period = config.get("supertrend_period", 10)
        self.st_multiplier = config.get("supertrend_multiplier", 3.0)
        self.rsi_period = config.get("rsi_period", 14)
        self.rsi_oversold = config.get("rsi_oversold", 35)
        self.rsi_overbought = config.get("rsi_overbought", 65)
        self.atr_period = config.get("atr_period", 14)
        self.sl_atr_mult = config.get("stop_loss_atr_multiplier", 1.5)
        self.target_atr_mult = config.get("target_atr_multiplier", 2.0)
        self.max_holding_minutes = config.get("max_holding_minutes", 180)

        # Previous indicator values for crossover detection
        self._prev_rsi: dict[str, float] = {}
        self._prev_supertrend_dir: dict[str, int] = {}

    def on_tick(self, tick: TickData) -> None:
        """Tick handler — not used for this strategy."""
        pass

    def on_bar(self, bar: BarData) -> None:
        """Bar handler — main signal generation."""
        if not self._enabled:
            return

        symbol = bar.symbol
        self.update_bar_data(symbol, bar)

        df = self.get_bars(symbol)
        min_bars_required = max(self.st_period, self.rsi_period, self.atr_period) + 5

        if len(df) < min_bars_required:
            return

        indicators = self._calculate_indicators(df)
        if indicators is None:
            return

        rsi = indicators["rsi"]
        supertrend_dir = indicators["supertrend_dir"]
        atr = indicators["atr"]

        prev_rsi = self._prev_rsi.get(symbol, 50)
        self._prev_rsi[symbol] = rsi
        self._prev_supertrend_dir[symbol] = supertrend_dir

        ltp = bar.close

        # Long signal: Supertrend bullish + RSI crosses above oversold
        long_condition = (
            supertrend_dir == 1 and prev_rsi <= self.rsi_oversold < rsi
        )
        # Short signal: Supertrend bearish + RSI crosses below overbought
        short_condition = (
            supertrend_dir == -1 and prev_rsi >= self.rsi_overbought > rsi
        )

        if long_condition:
            signal = Signal(
                timestamp=bar.timestamp,
                symbol=symbol,
                signal_type=SignalType.BUY,
                ltp=ltp,
                stop_loss=self.calculate_stop_loss(symbol, ltp, SignalType.BUY),
                target=self.calculate_target(symbol, ltp, SignalType.BUY),
                max_holding_minutes=self.max_holding_minutes,
                reason="ST bullish + RSI oversold crossover",
                indicators={
                    "rsi": rsi,
                    "supertrend_dir": supertrend_dir,
                    "atr": atr,
                },
            )
            self.emit_signal(signal)

        elif short_condition:
            signal = Signal(
                timestamp=bar.timestamp,
                symbol=symbol,
                signal_type=SignalType.SELL,
                ltp=ltp,
                stop_loss=self.calculate_stop_loss(symbol, ltp, SignalType.SELL),
                target=self.calculate_target(symbol, ltp, SignalType.SELL),
                max_holding_minutes=self.max_holding_minutes,
                reason="ST bearish + RSI overbought crossover",
                indicators={
                    "rsi": rsi,
                    "supertrend_dir": supertrend_dir,
                    "atr": atr,
                },
            )
            self.emit_signal(signal)

    def _calculate_indicators(self, df: pd.DataFrame) -> dict | None:
        """Calculate strategy indicators using pandas-ta."""
        try:
            # Ensure float64 dtype for pandas-ta compatibility
            h = df["high"].astype("float64")
            low = df["low"].astype("float64")
            c = df["close"].astype("float64")

            # RSI
            rsi_series = ta.rsi(c, length=self.rsi_period)
            if rsi_series is None or rsi_series.empty:
                return None
            rsi = float(rsi_series.iloc[-1])

            # Supertrend
            st = ta.supertrend(h, low, c, length=self.st_period, multiplier=self.st_multiplier)
            if st is None or st.empty:
                return None

            st_dir_col = f"SUPERTd_{self.st_period}_{self.st_multiplier}"
            supertrend_dir = int(st[st_dir_col].iloc[-1])

            # ATR
            atr_series = ta.atr(h, low, c, length=self.atr_period)
            if atr_series is None or atr_series.empty:
                return None
            atr = float(atr_series.iloc[-1])

            return {
                "rsi": rsi,
                "supertrend_dir": supertrend_dir,
                "atr": atr,
            }

        except Exception as e:
            logger.error("Indicator calculation error", error=str(e))
            return None

    def calculate_stop_loss(
        self, symbol: str, entry_price: float, side: SignalType
    ) -> float:
        """Calculate stop-loss using ATR."""
        df = self.get_bars(symbol)
        if len(df) < self.atr_period:
            return (
                entry_price * 0.99
                if side == SignalType.BUY
                else entry_price * 1.01
            )

        h = df["high"].astype("float64")
        low = df["low"].astype("float64")
        c = df["close"].astype("float64")
        atr_series = ta.atr(h, low, c, length=self.atr_period)
        if atr_series is None or atr_series.empty:
            return (
                entry_price * 0.99
                if side == SignalType.BUY
                else entry_price * 1.01
            )

        atr = float(atr_series.iloc[-1])

        if side == SignalType.BUY:
            return entry_price - (atr * self.sl_atr_mult)
        else:
            return entry_price + (atr * self.sl_atr_mult)

    def calculate_target(
        self, symbol: str, entry_price: float, side: SignalType
    ) -> float:
        """Calculate target using ATR."""
        df = self.get_bars(symbol)
        if len(df) < self.atr_period:
            return (
                entry_price * 1.02
                if side == SignalType.BUY
                else entry_price * 0.98
            )

        h = df["high"].astype("float64")
        low = df["low"].astype("float64")
        c = df["close"].astype("float64")
        atr_series = ta.atr(h, low, c, length=self.atr_period)
        if atr_series is None or atr_series.empty:
            return (
                entry_price * 1.02
                if side == SignalType.BUY
                else entry_price * 0.98
            )

        atr = float(atr_series.iloc[-1])

        if side == SignalType.BUY:
            return entry_price + (atr * self.target_atr_mult)
        else:
            return entry_price - (atr * self.target_atr_mult)
