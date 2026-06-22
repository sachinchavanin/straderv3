"""Tests for PaperTradeValidator — signal logging, P&L tracking, daily reports."""

import asyncio
import os
import tempfile
from datetime import datetime, date

import pytest

from strader3.models.trading import Signal, SignalType
from strader3.notifier.paper_trade_validator import PaperTradeValidator, DailyReport


def _make_signal(symbol: str, signal_type: SignalType, price: float, qty: int = 10) -> Signal:
    return Signal(
        symbol=symbol,
        signal_type=signal_type,
        ltp=price,
        quantity=qty,
        stop_loss=price * 0.95,
        target=price * 1.05,
        strategy_name="test_strategy",
        timestamp=datetime.now(),
    )


@pytest.fixture
def tmp_db():
    """Create a temporary database path."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def validator(tmp_db):
    return PaperTradeValidator(tmp_db)


class TestPaperTradeValidatorInit:
    def test_init(self, tmp_db):
        v = PaperTradeValidator(tmp_db)
        assert v.db_path == tmp_db
        assert v._open_trades == {}


class TestLogSignal:
    @pytest.mark.asyncio
    async def test_log_buy_signal(self, validator):
        signal = _make_signal("TCS", SignalType.BUY, 100.0, 10)
        sid = await validator.log_signal(signal)
        assert sid.startswith("psig_")
        assert "TCS" in validator._open_trades

    @pytest.mark.asyncio
    async def test_log_sell_signal(self, validator):
        signal = _make_signal("TCS", SignalType.SELL, 100.0, 10)
        sid = await validator.log_signal(signal)
        assert "TCS" in validator._open_trades
        assert validator._open_trades["TCS"]["direction"] == "SHORT"

    @pytest.mark.asyncio
    async def test_log_exit_closes_trade(self, validator):
        # Open a long
        buy = _make_signal("TCS", SignalType.BUY, 100.0, 10)
        await validator.log_signal(buy)

        # Close it
        sell = _make_signal("TCS", SignalType.EXIT_LONG, 110.0, 10)
        pnl = await validator._close_trade(sell)
        assert pnl == (110.0 - 100.0) * 10  # 100.0 profit
        assert "TCS" not in validator._open_trades

    @pytest.mark.asyncio
    async def test_log_multiple_signals(self, validator):
        for i, sym in enumerate(["TCS", "INFY", "HDFC"]):
            signal = _make_signal(sym, SignalType.BUY, 100.0 + i * 10, 10)
            await validator.log_signal(signal)

        history = await validator.get_trade_history()
        assert len(history) == 0  # No closed trades yet

    @pytest.mark.asyncio
    async def test_open_trades_tracking(self, validator):
        await validator.log_signal(_make_signal("TCS", SignalType.BUY, 100.0, 10))
        await validator.log_signal(_make_signal("INFY", SignalType.BUY, 200.0, 5))

        open_trades = await validator.get_open_trades()
        assert len(open_trades) == 2
        symbols = [t["symbol"] for t in open_trades]
        assert "TCS" in symbols
        assert "INFY" in symbols


class TestDailyReport:
    @pytest.mark.asyncio
    async def test_empty_report(self, validator):
        report = await validator.generate_daily_report()
        assert report.trades_count == 0
        assert report.total_pnl == 0.0
        assert report.win_rate == 0.0

    @pytest.mark.asyncio
    async def test_report_with_trades(self, validator):
        # Create some round-trip trades
        for i in range(3):
            buy = _make_signal(f"SYM{i}", SignalType.BUY, 100.0, 10)
            await validator.log_signal(buy)

            # Close at profit for first 2, loss for third
            exit_price = 110.0 if i < 2 else 90.0
            sell = _make_signal(f"SYM{i}", SignalType.EXIT_LONG, exit_price, 10)
            await validator.log_signal(sell)

        report = await validator.generate_daily_report()
        assert report.trades_count == 3
        assert report.wins_count == 2
        assert report.losses_count == 1
        assert report.win_rate == 2 / 3
        assert report.total_pnl == (10 * 10) + (10 * 10) + (-10 * 10)  # 100 + 100 - 100 = 100

    @pytest.mark.asyncio
    async def test_max_drawdown(self, validator):
        # Trade 1: +100, Trade 2: -200, Trade 3: +50
        scenarios = [
            ("A", 100.0, 110.0),  # +100
            ("B", 200.0, 180.0),  # -200
            ("C", 300.0, 305.0),  # +50
        ]
        for sym, entry, exit_p in scenarios:
            buy = _make_signal(sym, SignalType.BUY, entry, 10)
            await validator.log_signal(buy)
            sell = _make_signal(sym, SignalType.EXIT_LONG, exit_p, 10)
            await validator.log_signal(sell)

        report = await validator.generate_daily_report()
        # Cumulative: +100, -100, -50
        # Peak: 100, 100, 100
        # Max DD: 100 - (-100) = 200
        assert report.max_drawdown == 200.0

    @pytest.mark.asyncio
    async def test_report_persisted(self, validator):
        buy = _make_signal("TCS", SignalType.BUY, 100.0, 10)
        await validator.log_signal(buy)
        sell = _make_signal("TCS", SignalType.EXIT_LONG, 110.0, 10)
        await validator.log_signal(sell)

        report = await validator.generate_daily_report()
        assert report.trades_count == 1

        # Verify it was persisted
        history = await validator.get_trade_history()
        assert len(history) == 1
        assert history[0]["gross_pnl"] == 100.0


class TestFormatReport:
    def test_format_empty_report(self, validator):
        report = DailyReport(
            report_date="2026-01-15",
            total_pnl=0.0,
            trades_count=0,
            wins_count=0,
            losses_count=0,
            win_rate=0.0,
            avg_pnl=0.0,
            max_drawdown=0.0,
            avg_win=0.0,
            avg_loss=0.0,
        )
        text = validator.format_report_text(report)
        assert "2026-01-15" in text
        assert "Total P&L" in text
        assert "Trades:         0" in text

    def test_format_report_with_trades(self, validator):
        report = DailyReport(
            report_date="2026-01-15",
            total_pnl=150.0,
            trades_count=2,
            wins_count=1,
            losses_count=1,
            win_rate=0.5,
            avg_pnl=75.0,
            max_drawdown=50.0,
            avg_win=200.0,
            avg_loss=-50.0,
        )
        text = validator.format_report_text(report)
        assert "150" in text
        assert "Win Rate" in text
        assert "Max Drawdown" in text
