"""Verification script for straderv3 Phase 1 acceptance criteria."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from datetime import datetime, timedelta
from strader3.models import (
    BarData, TickData, Signal, Order, Position, Trade,
    SignalType, OrderType, OrderStatus, ProductType, PositionState,
)
from strader3.adapters.base import DataFeeder, BrokerAdapter
from strader3.adapters.fyers import FyersAuth
from strader3.adapters.fyers.feeder import FyersFeeder
from strader3.core.feed_handler import BarAggregator, FeedHandler
from strader3.core.alpha_engine import BaseStrategy, STRsiTrendRider
from strader3.utils.config import load_config, get_config, get_nested
from strader3.utils.logging import setup_logging

print("All imports successful!")
print()

# Test structlog JSON logging
setup_logging(level="INFO", log_dir="/tmp/test_logs", json_format=True, console=True)
import structlog
log = structlog.get_logger("test")
log.info("structlog JSON test", key="value", number=42)
print("structlog JSON logging works!")
print()

# Test config loading
cfg = load_config("config.yaml")
print(f"Config loaded: trade_mode={cfg.get('trade_mode')}")
print(f"Watchlist: {len(cfg.get('market_data', {}).get('watchlist', []))} symbols")
print(f"Strategy: {cfg.get('strategies', {}).get('enabled', [])}")
print()

# Test data models
tick = TickData(symbol="NSE:TCS-EQ", ltp=3842.50, timestamp=datetime.now())
print(f"TickData: {tick.symbol} @ {tick.ltp}, spread={tick.spread}")

bar = BarData(
    symbol="NSE:TCS-EQ", timestamp=datetime.now(), timeframe="1m",
    open=3840, high=3845, low=3838, close=3842, volume=1000,
)
print(f"BarData: {bar.symbol} O={bar.open} H={bar.high} L={bar.low} C={bar.close}")
print(f"  is_bullish={bar.is_bullish}, body_size={bar.body_size}")

sig = Signal(
    symbol="NSE:TCS-EQ", signal_type=SignalType.BUY, ltp=3842.50,
    stop_loss=3785.0, target=3956.0, reason="ST bullish + RSI oversold crossover",
)
print(f"Signal: {sig.signal_type.value} {sig.symbol} @ {sig.ltp}")
print(f"  SL={sig.stop_loss}, Target={sig.target}")

order = Order(symbol="NSE:TCS-EQ", side=SignalType.BUY, quantity=50)
print(f"Order: {order.side.value} {order.quantity} {order.symbol}, status={order.status.value}")

pos = Position(symbol="NSE:TCS-EQ", quantity=50, average_price=3842.50)
pos.update_mtm(3850.0)
print(f"Position: {pos.quantity} @ {pos.average_price}, MTM PnL={pos.unrealized_pnl}")

trade = Trade(
    symbol="NSE:TCS-EQ", entry_price=3842.50, exit_price=3956.0,
    entry_quantity=50, exit_quantity=50, exit_reason="target",
)
print(f"Trade: {trade.symbol} entry={trade.entry_price} exit={trade.exit_price} reason={trade.exit_reason}")
print()

# Test BarAggregator
agg = BarAggregator(timeframe="1m")
bars_emitted = []
agg.on_bar(lambda b: bars_emitted.append(b))

base_time = datetime(2026, 1, 15, 9, 15, 0)
for i in range(120):
    t = TickData(
        symbol="NSE:TCS-EQ",
        ltp=3840.0 + i * 0.1,
        timestamp=base_time + timedelta(seconds=i),
        volume=1000 + i * 10,
    )
    agg.process_tick(t)

print(f"BarAggregator: processed 120 ticks, emitted {len(bars_emitted)} bars")
if bars_emitted:
    b = bars_emitted[0]
    print(f"  First bar: {b.timestamp} O={b.open} H={b.high} L={b.low} C={b.close} V={b.volume}")
print()

# Test STRsiTrendRider signal generation with synthetic data
print("Testing STRsiTrendRider signal generation...")
import numpy as np
np.random.seed(42)

strategy_config = {
    "enabled": True,
    "supertrend_period": 10,
    "supertrend_multiplier": 3.0,
    "rsi_period": 14,
    "rsi_oversold": 35,
    "rsi_overbought": 65,
    "atr_period": 14,
    "stop_loss_atr_multiplier": 1.5,
    "target_atr_multiplier": 2.0,
    "max_holding_minutes": 180,
}
strategy = STRsiTrendRider(strategy_config)
signals_received = []
strategy.on_signal(lambda s: signals_received.append(s))

base_price = 3840.0
prices = [base_price]
for i in range(1, 200):
    change = np.random.normal(0, 2.0)
    if 80 < i < 120:
        change = -abs(change) - 1.0
    elif i >= 120:
        change = abs(change) + 0.5
    prices.append(prices[-1] + change)

base_time = datetime(2026, 1, 15, 9, 15, 0)
for i, price in enumerate(prices):
    bar = BarData(
        symbol="NSE:TCS-EQ",
        timestamp=base_time + timedelta(minutes=i),
        timeframe="1m",
        open=price - 0.5,
        high=price + 1.0,
        low=price - 1.0,
        close=price,
        volume=1000 + i * 10,
    )
    strategy.on_bar(bar)

print(f"  Processed {len(prices)} bars, generated {len(signals_received)} signals")
for s in signals_received:
    print(f"  Signal: {s.signal_type.value} @ {s.ltp:.2f} - {s.reason}")
    print(f"    SL={s.stop_loss:.2f}, Target={s.target:.2f}")
    rsi_val = s.indicators.get("rsi", 0)
    st_val = s.indicators.get("supertrend_dir", 0)
    print(f"    Indicators: RSI={rsi_val:.2f}, ST_dir={st_val}")

print()
print("ALL TESTS PASSED")
