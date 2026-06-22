"""Integration tests for T4 features wired into TradingEngine.

Tests that AlertManager, PaperTradeValidator, MarketPhaseChecker,
and WatchlistManager are correctly integrated into the engine.
"""

import asyncio
import os
import tempfile
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strader3.models.trading import Signal, SignalType


@pytest.fixture
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def base_config():
    return {
        "trade_mode": "paper",
        "database": {"path": "data/trades.db"},
        "market_data": {
            "watchlist": ["NSE:TCS-EQ", "NSE:INFY-EQ"],
            "bar_interval": "1m",
        },
        "strategies": {
            "enabled": [],
            "st_rsi_trend_rider": {"enabled": False},
        },
        "portfolio": {
            "allocation_pct": 50.0,
            "max_active_trades": 3,
            "cooldown_seconds": 300,
        },
        "risk": {
            "max_daily_loss_pct": 1.0,
            "entry_start_time": "09:20",
            "entry_end_time": "15:00",
            "force_exit_time": "15:15",
        },
        "schedule": {
            "market_open": "09:15",
            "market_close": "15:30",
            "holidays": ["2026-01-26"],
        },
        "discord": {
            "enabled": True,
            "webhook_url": None,
            "cadence": {"out_of_hours": 60},
        },
    }


class TestEngineT4Integration:
    def test_engine_inits_all_t4_modules(self, base_config, tmp_db):
        from strader3.core.trading_engine import TradingEngine

        engine = TradingEngine(config=base_config, db_path=tmp_db)
        assert engine.alert_manager is not None
        assert engine.paper_trade_validator is not None
        assert engine.market_phase is not None
        assert engine.watchlist_manager is not None

    def test_engine_watchlist_loaded(self, base_config, tmp_db):
        from strader3.core.trading_engine import TradingEngine

        engine = TradingEngine(config=base_config, db_path=tmp_db)
        assert len(engine.watchlist_manager.symbols) == 2
        assert "NSE:TCS-EQ" in engine.watchlist_manager.symbols

    def test_engine_market_phase_loaded_holidays(self, base_config, tmp_db):
        from strader3.core.trading_engine import TradingEngine
        from datetime import date

        engine = TradingEngine(config=base_config, db_path=tmp_db)
        assert engine.market_phase.is_holiday(date(2026, 1, 26))

    def test_engine_get_status_includes_t4_fields(self, base_config, tmp_db):
        from strader3.core.trading_engine import TradingEngine

        engine = TradingEngine(config=base_config, db_path=tmp_db)
        status = engine.get_status()

        assert "alert_metrics" in status
        assert "market_phase" in status
        assert "market_status" in status
        assert "can_enter" in status
        assert "force_exit_active" in status
        assert "watchlist_symbols" in status

    def test_engine_alert_manager_not_configured_without_webhook(self, base_config, tmp_db):
        from strader3.core.trading_engine import TradingEngine

        engine = TradingEngine(config=base_config, db_path=tmp_db)
        assert not engine.alert_manager.is_configured

    def test_engine_alert_manager_configured_with_webhook(self, base_config, tmp_db):
        from strader3.core.trading_engine import TradingEngine

        base_config["discord"]["webhook_url"] = "https://discord.com/api/webhooks/123/abc"
        engine = TradingEngine(config=base_config, db_path=tmp_db)
        assert engine.alert_manager.is_configured

    @pytest.mark.asyncio
    async def test_engine_initialize_creates_db(self, base_config, tmp_db):
        from strader3.core.trading_engine import TradingEngine

        engine = TradingEngine(config=base_config, db_path=tmp_db)
        await engine.initialize()
        assert os.path.exists(tmp_db)

    @pytest.mark.asyncio
    async def test_engine_paper_trade_validator_logs_signals(self, base_config, tmp_db):
        from strader3.core.trading_engine import TradingEngine

        engine = TradingEngine(config=base_config, db_path=tmp_db)
        await engine.initialize()

        signal = Signal(
            symbol="NSE:TCS-EQ",
            signal_type=SignalType.BUY,
            ltp=3500.0,
            quantity=10,
            stop_loss=3450.0,
            target=3600.0,
            strategy_name="test",
            timestamp=datetime.now(),
        )
        await engine.paper_trade_validator.log_signal(signal)

        open_trades = await engine.paper_trade_validator.get_open_trades()
        assert len(open_trades) == 1
        assert open_trades[0]["symbol"] == "NSE:TCS-EQ"

    def test_engine_market_phase_blocks_pre_open_entries(self, base_config, tmp_db):
        from strader3.core.trading_engine import TradingEngine

        engine = TradingEngine(config=base_config, db_path=tmp_db)
        can_enter, reason = engine.market_phase.can_enter_position(time(9, 15))
        assert not can_enter

    def test_engine_watchlist_reload_count(self, base_config, tmp_db):
        from strader3.core.trading_engine import TradingEngine

        engine = TradingEngine(config=base_config, db_path=tmp_db)
        assert engine.watchlist_manager.reload_count == 0

    @pytest.mark.asyncio
    async def test_engine_shutdown_closes_alert_manager(self, base_config, tmp_db):
        from strader3.core.trading_engine import TradingEngine

        engine = TradingEngine(config=base_config, db_path=tmp_db)
        await engine.initialize()
        await engine.shutdown()
        # Should complete without errors
