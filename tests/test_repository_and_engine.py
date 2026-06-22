"""Tests for repository layer and trade pivot CSV export."""

import asyncio
import csv
import os
import tempfile

import pytest

from strader3.core.repository import (
    DailyPnlRepository,
    MigrationManager,
    OrderRepository,
    PositionRepository,
    SignalRepository,
    TradeRepository,
)
from strader3.core.trading_engine import TradingEngine
from strader3.models.trading import (
    Order,
    OrderStatus,
    Position,
    PositionState,
    Signal,
    SignalType,
    Trade,
)
from strader3.utils.config import load_config
from strader3.utils.trade_pivot import export_trades_csv


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def db_path() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        yield os.path.join(tmp, "test_trades.db")  # noqa: S108


@pytest.fixture
async def signal_repo(db_path: str) -> SignalRepository:
    return SignalRepository(db_path)


@pytest.fixture
async def trade_repo(db_path: str) -> TradeRepository:
    return TradeRepository(db_path)


@pytest.fixture
async def position_repo(db_path: str) -> PositionRepository:
    return PositionRepository(db_path)


@pytest.fixture
async def daily_pnl_repo(db_path: str) -> DailyPnlRepository:
    return DailyPnlRepository(db_path)


@pytest.fixture
async def order_repo(db_path: str) -> OrderRepository:
    return OrderRepository(db_path)


def make_signal(**overrides) -> Signal:
    defaults: dict = dict(
        symbol="RELIANCE",
        signal_type=SignalType.BUY,
        strategy_name="test",
        ltp=2500.0,
        stop_loss=2450.0,
        target=2600.0,
        quantity=10,
    )
    defaults.update(overrides)
    return Signal(**defaults)


def make_trade(**overrides) -> Trade:
    defaults: dict = dict(
        symbol="RELIANCE",
        strategy_name="test",
        entry_price=2500.0,
        entry_quantity=10,
        exit_price=2600.0,
        exit_quantity=10,
        exit_reason="target",
        gross_pnl=1000.0,
        charges=20.0,
        net_pnl=980.0,
    )
    defaults.update(overrides)
    return Trade(**defaults)


# =====================================================================
# SignalRepository
# =====================================================================


class TestSignalRepository:
    @pytest.mark.asyncio
    async def test_save_and_retrieve(self, signal_repo: SignalRepository) -> None:
        signal = make_signal()
        await signal_repo.save(signal)
        result = await signal_repo.get_by_id(signal.id)
        assert result is not None
        assert result.symbol == "RELIANCE"
        assert result.signal_type == SignalType.BUY

    @pytest.mark.asyncio
    async def test_get_by_symbol(self, signal_repo: SignalRepository) -> None:
        await signal_repo.save(make_signal(symbol="RELIANCE"))
        await signal_repo.save(make_signal(symbol="RELIANCE"))
        results = await signal_repo.get_by_symbol("RELIANCE")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_get_all(self, signal_repo: SignalRepository) -> None:
        await signal_repo.save(make_signal(symbol="A"))
        await signal_repo.save(make_signal(symbol="B"))
        results = await signal_repo.get_all()
        assert len(results) == 2


# =====================================================================
# TradeRepository
# =====================================================================


class TestTradeRepository:
    @pytest.mark.asyncio
    async def test_save_and_get_by_symbol(self, trade_repo: TradeRepository) -> None:
        trade = make_trade()
        await trade_repo.save(trade)
        results = await trade_repo.get_by_symbol("RELIANCE")
        assert len(results) == 1
        assert results[0]["gross_pnl"] == 1000.0

    @pytest.mark.asyncio
    async def test_get_pnl_summary(self, trade_repo: TradeRepository) -> None:
        await trade_repo.save(make_trade(gross_pnl=1000.0, net_pnl=980.0))
        await trade_repo.save(make_trade(gross_pnl=-500.0, net_pnl=-520.0))
        summary = await trade_repo.get_pnl_summary()
        assert summary["total_trades"] == 2
        assert summary["wins"] == 1
        assert summary["losses"] == 1


# =====================================================================
# PositionRepository
# =====================================================================


class TestPositionRepository:
    @pytest.mark.asyncio
    async def test_save_and_get(self, position_repo: PositionRepository) -> None:
        position = Position(
            symbol="RELIANCE",
            state=PositionState.ACTIVE,
            quantity=10,
            average_price=2500.0,
        )
        await position_repo.save(position)
        result = await position_repo.get_by_symbol("RELIANCE")
        assert result is not None
        assert result["quantity"] == 10

    @pytest.mark.asyncio
    async def test_delete(self, position_repo: PositionRepository) -> None:
        position = Position(symbol="RELIANCE", quantity=10)
        await position_repo.save(position)
        await position_repo.delete("RELIANCE")
        result = await position_repo.get_by_symbol("RELIANCE")
        assert result is None


# =====================================================================
# DailyPnlRepository
# =====================================================================


class TestDailyPnlRepository:
    @pytest.mark.asyncio
    async def test_save_and_get(self, daily_pnl_repo: DailyPnlRepository) -> None:
        await daily_pnl_repo.save_daily_pnl(
            date="2026-06-22",
            realized_pnl=5000.0,
            unrealized_pnl=200.0,
            trades_count=3,
            wins_count=2,
            losses_count=1,
        )
        result = await daily_pnl_repo.get_by_date("2026-06-22")
        assert result is not None
        assert result["total_pnl"] == 5200.0

    @pytest.mark.asyncio
    async def test_upsert(self, daily_pnl_repo: DailyPnlRepository) -> None:
        await daily_pnl_repo.save_daily_pnl(date="2026-06-22", realized_pnl=100.0, unrealized_pnl=0.0)
        await daily_pnl_repo.save_daily_pnl(date="2026-06-22", realized_pnl=200.0, unrealized_pnl=0.0)
        result = await daily_pnl_repo.get_by_date("2026-06-22")
        assert result["realized_pnl"] == 200.0


# =====================================================================
# OrderRepository
# =====================================================================


class TestOrderRepository:
    @pytest.mark.asyncio
    async def test_save_and_get(self, order_repo: OrderRepository) -> None:
        order = Order(symbol="RELIANCE", side=SignalType.BUY, quantity=10)
        await order_repo.save(order)
        result = await order_repo.get_by_id(order.id)
        assert result is not None
        assert result["symbol"] == "RELIANCE"

    @pytest.mark.asyncio
    async def test_get_by_status(self, order_repo: OrderRepository) -> None:
        order = Order(symbol="A", status=OrderStatus.FILLED)
        await order_repo.save(order)
        results = await order_repo.get_by_status("FILLED")
        assert len(results) == 1


# =====================================================================
# MigrationManager
# =====================================================================


class TestMigrationManager:
    @pytest.mark.asyncio
    async def test_migration_applies(self, db_path: str) -> None:
        mgr = MigrationManager(db_path)
        await mgr.migrate()
        # Verify signals table exists
        import aiosqlite

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT version FROM schema_migrations") as cursor:
                rows = await cursor.fetchall()
                versions = [r["version"] for r in rows]
                assert 1 in versions

    @pytest.mark.asyncio
    async def test_migration_idempotent(self, db_path: str) -> None:
        mgr = MigrationManager(db_path)
        await mgr.migrate()
        await mgr.migrate()  # Should not fail
        # Should still only have version 1


# =====================================================================
# Trade Pivot CSV Export
# =====================================================================


class TestTradePivotCsv:
    @pytest.mark.asyncio
    async def test_export_creates_csv(self, trade_repo: TradeRepository) -> None:
        trade = make_trade()
        await trade_repo.save(trade)

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "pivots.csv")
            result = await export_trades_csv(
                str(trade_repo.db_path), csv_path
            )
            assert os.path.exists(result)

            with open(result) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 1
                assert rows[0]["symbol"] == "RELIANCE"
                assert rows[0]["gross_pnl"] == "1000.0"

    @pytest.mark.asyncio
    async def test_export_empty_db(self, db_path: str) -> None:
        # Initialize the DB first
        from strader3.core.db import init_db
        await init_db(db_path)

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "pivots.csv")
            result = await export_trades_csv(db_path, csv_path)
            assert os.path.exists(result)

            with open(result) as f:
                reader = csv.reader(f)
                rows = list(reader)
                assert len(rows) == 1  # Header only


# =====================================================================
# TradingEngine Integration
# =====================================================================


class TestTradingEngine:
    @pytest.mark.asyncio
    async def test_engine_initializes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.yaml")
            # Copy config
            import shutil

            src_config = os.path.join(
                os.path.dirname(__file__), "..", "..", "config.yaml"
            )
            if os.path.exists(src_config):
                shutil.copy(src_config, config_path)
            else:
                # Write minimal config
                with open(config_path, "w") as f:
                    f.write("trade_mode: paper\n")

            config = load_config(config_path)
            db_path = os.path.join(tmp, "test.db")
            engine = TradingEngine(config=config, db_path=db_path)
            await engine.initialize()

            status = engine.get_status()
            assert status["running"] is True
            assert os.path.exists(db_path)

            await engine.shutdown()
