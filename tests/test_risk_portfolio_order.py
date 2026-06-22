"""Tests for Risk Manager, Portfolio Manager, Order Manager, and SQLite schema."""

import asyncio
import os
import tempfile
from datetime import datetime, time
from unittest.mock import patch

import pytest

from strader3.core.db import init_db
from strader3.core.order_manager import OrderManager
from strader3.core.portfolio_manager import (
    CapitalAllocator,
    InvalidTransitionError,
    PortfolioManager,
    SymbolState,
)
from strader3.core.risk_manager import RiskManager
from strader3.models.trading import (
    Order,
    OrderStatus,
    Position,
    PositionState,
    Signal,
    SignalType,
)


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def risk_manager() -> RiskManager:
    rm = RiskManager()
    rm.set_capital(1_000_000.0)
    return rm


@pytest.fixture
def sample_signal() -> Signal:
    return Signal(
        symbol="RELIANCE",
        signal_type=SignalType.BUY,
        strategy_name="STRsiTrendRider",
        ltp=2500.0,
        stop_loss=2450.0,
        target=2600.0,
        quantity=10,
        rms_approved=False,
    )


@pytest.fixture
def exit_signal() -> Signal:
    return Signal(
        symbol="RELIANCE",
        signal_type=SignalType.EXIT_LONG,
        strategy_name="STRsiTrendRider",
        ltp=2550.0,
        quantity=10,
    )


@pytest.fixture
def capital_allocator() -> CapitalAllocator:
    return CapitalAllocator(
        total_capital=1_000_000.0,
        allocation_pct=5.0,
        max_active_trades=3,
        max_sector_exposure_pct=30.0,
    )


@pytest.fixture
def portfolio_manager(capital_allocator: CapitalAllocator) -> PortfolioManager:
    return PortfolioManager(
        capital_allocator=capital_allocator,
        sector_map={"ENERGY": ["RELIANCE", "ONGC"]},
        cooldown_seconds=300,
    )


@pytest.fixture
def db_path() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        yield os.path.join(tmp, "test_trades.db")


@pytest.fixture
async def order_manager(db_path: str) -> OrderManager:
    om = OrderManager(db_path=db_path, paper_trade=True)
    await om.initialize()
    return om


def make_rm() -> RiskManager:
    rm = RiskManager()
    rm.set_capital(1_000_000.0)
    return rm


def make_signal(**overrides) -> Signal:
    defaults: dict[str, object] = dict(
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


# =====================================================================
# Risk Manager — Daily Loss Limit
# =====================================================================


class TestRiskManagerDailyLoss:
    def test_within_limit(self, risk_manager: RiskManager) -> None:
        risk_manager.update_pnl(-5000.0, 0.0)
        assert risk_manager._risk_mode == "NORMAL"

    def test_breach_triggers_risk_off(self, risk_manager: RiskManager) -> None:
        # 2% of 1M = 20000
        risk_manager.update_pnl(-25000.0, 0.0)
        assert risk_manager._risk_mode == "RISK_OFF"
        assert risk_manager._kill_switch_tripped is True

    def test_exactly_at_limit(self, risk_manager: RiskManager) -> None:
        risk_manager.update_pnl(-20000.0, 0.0)
        # Exactly at limit should NOT trigger (strictly less than)
        assert risk_manager._risk_mode == "NORMAL"


# =====================================================================
# Risk Manager — Kill Switch (Consecutive Losses)
# =====================================================================


class TestRiskManagerKillSwitch:
    def test_three_consecutive_losses_triggers_kill_switch(self) -> None:
        rm = make_rm()
        rm.record_trade_result(-100.0)
        rm.record_trade_result(-200.0)
        rm.record_trade_result(-50.0)
        assert rm._kill_switch_tripped is True
        assert rm._risk_mode == "RISK_OFF"

    def test_two_losses_then_wipe_resets(self) -> None:
        rm = make_rm()
        rm.record_trade_result(-100.0)
        rm.record_trade_result(-200.0)
        assert rm._consecutive_losses == 2
        rm.record_trade_result(50.0)
        assert rm._consecutive_losses == 0

    def test_kill_switch_blocks_new_entries(self) -> None:
        rm = make_rm()
        rm._kill_switch_tripped = True
        rm._risk_mode = "RISK_OFF"
        signal = make_signal()
        approved, reason = rm.validate_signal(signal, 0.0, 0)
        assert approved is False
        assert "Kill switch" in reason

    def test_reset_kill_switch(self) -> None:
        rm = make_rm()
        rm._kill_switch_tripped = True
        rm._risk_mode = "RISK_OFF"
        rm._consecutive_losses = 3
        rm.reset_kill_switch()
        assert rm._kill_switch_tripped is False
        assert rm._risk_mode == "NORMAL"
        assert rm._consecutive_losses == 0


# =====================================================================
# Risk Manager — Time Guards
# =====================================================================


class TestRiskManagerTimeGuards:
    def _validate_at(self, rm: RiskManager, hour: int, minute: int) -> tuple:
        signal = make_signal()
        with patch("strader3.core.risk_manager.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, hour, minute)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            return rm.validate_signal(signal, 0.0, 0)

    def test_entry_within_window(self, risk_manager: RiskManager) -> None:
        approved, reason = self._validate_at(risk_manager, 10, 0)
        assert approved is True

    def test_entry_before_window(self, risk_manager: RiskManager) -> None:
        approved, reason = self._validate_at(risk_manager, 9, 0)
        assert approved is False
        assert "Outside entry window" in reason

    def test_entry_after_window(self, risk_manager: RiskManager) -> None:
        approved, reason = self._validate_at(risk_manager, 15, 5)
        assert approved is False
        assert "Outside entry window" in reason

    def test_force_exit_time(self, risk_manager: RiskManager) -> None:
        with patch("strader3.core.risk_manager.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 15, 15)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert risk_manager.check_force_exit_time() is True

    def test_before_force_exit_time(self, risk_manager: RiskManager) -> None:
        with patch("strader3.core.risk_manager.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 15, 10)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert risk_manager.check_force_exit_time() is False


# =====================================================================
# Risk Manager — Position / Order Limits
# =====================================================================


class TestRiskManagerPositionLimits:
    def test_max_open_positions(self, risk_manager: RiskManager) -> None:
        signal = make_signal()
        approved, reason = risk_manager.validate_signal(signal, 0.0, 3)
        assert approved is False
        assert "Max open positions" in reason

    def test_max_quantity_exceeded(self, risk_manager: RiskManager) -> None:
        signal = make_signal(quantity=1500)
        signal.rms_approved = False
        approved, reason = risk_manager.validate_signal(signal, 0.0, 0)
        assert approved is False
        assert "max 1000" in reason

    def test_max_order_value_exceeded(self, risk_manager: RiskManager) -> None:
        signal = make_signal(ltp=60000.0, quantity=1)
        approved, reason = risk_manager.validate_signal(signal, 0.0, 0)
        assert approved is False
        assert "Order value" in reason

    def test_total_exposure_cap(self, risk_manager: RiskManager) -> None:
        # 50% of 1M = 500000, max_order_value = 50000
        # qty=10 at 2500 = 25000 order value (under 50000 max)
        signal = make_signal(quantity=10, ltp=2500.0)
        # Current exposure = 20000, new order = 25000, total = 45000 < 500000 -> OK
        approved, _ = risk_manager.validate_signal(signal, 20_000.0, 0)
        assert approved is True

    def test_total_exposure_exceeded(self, risk_manager: RiskManager) -> None:
        # Need quantity that passes max_order_value (50000) but fails exposure (500000)
        # qty=10 at 2500 = 25000 order value (under 50000 max)
        signal = make_signal(quantity=10, ltp=2500.0)
        # Current exposure = 490000, new order = 25000, total = 515000 > 500000 -> REJECT
        approved, reason = risk_manager.validate_signal(signal, 490_000.0, 0)
        assert approved is False
        assert "exposure" in reason.lower()

    def test_missing_stop_loss(self, risk_manager: RiskManager) -> None:
        signal = make_signal(stop_loss=0.0)
        approved, reason = risk_manager.validate_signal(signal, 0.0, 0)
        assert approved is False
        assert "Stop-loss" in reason

    def test_missing_target(self, risk_manager: RiskManager) -> None:
        signal = make_signal(target=0.0)
        approved, reason = risk_manager.validate_signal(signal, 0.0, 0)
        assert approved is False
        assert "Target" in reason

    def test_flat_only_blocks_entries(self, risk_manager: RiskManager) -> None:
        risk_manager.set_risk_mode("FLAT_ONLY")
        signal = make_signal()
        approved, reason = risk_manager.validate_signal(signal, 0.0, 0)
        assert approved is False
        assert "Flat-only" in reason

    def test_exit_allowed_in_flat_only(self, risk_manager: RiskManager) -> None:
        risk_manager.set_risk_mode("FLAT_ONLY")
        signal = Signal(
            symbol="RELIANCE",
            signal_type=SignalType.EXIT_LONG,
            quantity=10,
        )
        approved, reason = risk_manager.validate_signal(signal, 0.0, 0)
        assert approved is True


# =====================================================================
# Risk Manager — Position Checks
# =====================================================================


class TestRiskManagerPositionChecks:
    def test_stop_loss_hit_long(self) -> None:
        pos = Position(
            symbol="RELIANCE",
            state=PositionState.ACTIVE,
            quantity=10,
            average_price=2500.0,
            stop_loss=2450.0,
            target=2600.0,
        )
        assert RiskManager.check_stop_loss(pos, 2440.0) is True
        assert RiskManager.check_stop_loss(pos, 2460.0) is False

    def test_stop_loss_hit_short(self) -> None:
        pos = Position(
            symbol="RELIANCE",
            state=PositionState.ACTIVE,
            quantity=-10,
            average_price=2500.0,
            stop_loss=2550.0,
            target=2400.0,
        )
        assert RiskManager.check_stop_loss(pos, 2560.0) is True
        assert RiskManager.check_stop_loss(pos, 2540.0) is False

    def test_target_hit_long(self) -> None:
        pos = Position(
            symbol="RELIANCE",
            state=PositionState.ACTIVE,
            quantity=10,
            average_price=2500.0,
            stop_loss=2450.0,
            target=2600.0,
        )
        assert RiskManager.check_target(pos, 2610.0) is True
        assert RiskManager.check_target(pos, 2590.0) is False

    def test_target_hit_short(self) -> None:
        pos = Position(
            symbol="RELIANCE",
            state=PositionState.ACTIVE,
            quantity=-10,
            average_price=2500.0,
            stop_loss=2550.0,
            target=2400.0,
        )
        assert RiskManager.check_target(pos, 2390.0) is True
        assert RiskManager.check_target(pos, 2410.0) is False

    def test_circuit_detection(self) -> None:
        assert RiskManager.check_circuit(bid_qty=0, ask_qty=100) is True
        assert RiskManager.check_circuit(bid_qty=100, ask_qty=0) is True
        assert RiskManager.check_circuit(bid_qty=0, ask_qty=0) is True
        assert RiskManager.check_circuit(bid_qty=100, ask_qty=100) is False


# =====================================================================
# Capital Allocator
# =====================================================================


class TestCapitalAllocator:
    def test_allocation_based_sizing(self) -> None:
        ca = CapitalAllocator(total_capital=1_000_000, allocation_pct=5.0)
        qty = ca.calculate_quantity(ltp=2500.0)
        # 5% of 1M = 50000 / 2500 = 20
        assert qty == 20

    def test_allocation_cannot_exceed_available(self) -> None:
        ca = CapitalAllocator(total_capital=100_000, allocation_pct=5.0, max_active_trades=1)
        ca._used_capital = 96_000
        ca._active_trades = 0
        # Available = 4000, allocation = 5000, so only 4000 / 2500 = 1
        qty = ca.calculate_quantity(ltp=2500.0)
        assert qty == 1

    def test_no_allocation_trades_full(self) -> None:
        ca = CapitalAllocator(total_capital=1_000_000, max_active_trades=1)
        ca._active_trades = 1
        assert ca.can_allocate is False
        assert ca.calculate_quantity(ltp=100.0) == 0

    def test_check_sector_limit(self) -> None:
        ca = CapitalAllocator(total_capital=1_000_000, max_sector_exposure_pct=30.0)
        assert ca.check_sector_limit("ENERGY", 200_000.0) is True
        assert ca.check_sector_limit("ENERGY", 400_000.0) is False

    def test_allocate_and_release(self) -> None:
        ca = CapitalAllocator(total_capital=1_000_000, max_active_trades=3)
        assert ca.allocate("RELIANCE", 50_000.0, "ENERGY") is True
        assert ca._used_capital == 50_000.0
        assert ca._active_trades == 1
        ca.release("RELIANCE", 50_000.0, "ENERGY")
        assert ca._used_capital == 0.0
        assert ca._active_trades == 0

    def test_allocate_fails_when_full(self) -> None:
        ca = CapitalAllocator(total_capital=1_000_000, max_active_trades=1)
        ca._active_trades = 1
        assert ca.allocate("RELIANCE", 50_000.0) is False

    def test_allocate_exceeds_capital(self) -> None:
        ca = CapitalAllocator(total_capital=100_000)
        assert ca.allocate("RELIANCE", 200_000.0) is False


# =====================================================================
# Portfolio Manager — State Machine
# =====================================================================


class TestPortfolioManagerStateMachine:
    def test_initial_state_is_idle(self, portfolio_manager: PortfolioManager) -> None:
        assert portfolio_manager.get_symbol_state("RELIANCE") == SymbolState.IDLE

    def test_valid_transition_idle_to_pending_entry(
        self, portfolio_manager: PortfolioManager
    ) -> None:
        portfolio_manager.transition("RELIANCE", SymbolState.PENDING_ENTRY)
        assert portfolio_manager.get_symbol_state("RELIANCE") == SymbolState.PENDING_ENTRY

    def test_invalid_transition_raises(self, portfolio_manager: PortfolioManager) -> None:
        with pytest.raises(InvalidTransitionError):
            portfolio_manager.transition("RELIANCE", SymbolState.ACTIVE)

    def test_full_lifecycle(
        self, portfolio_manager: PortfolioManager
    ) -> None:
        sym = "RELIANCE"
        portfolio_manager.transition(sym, SymbolState.PENDING_ENTRY)
        portfolio_manager.transition(sym, SymbolState.ACTIVE)
        portfolio_manager.transition(sym, SymbolState.PENDING_EXIT)
        portfolio_manager.transition(sym, SymbolState.COOLDOWN)
        portfolio_manager.transition(sym, SymbolState.IDLE)
        assert portfolio_manager.get_symbol_state(sym) == SymbolState.IDLE

    def test_cooldown_to_pending_entry_raises(
        self, portfolio_manager: PortfolioManager
    ) -> None:
        portfolio_manager._symbol_states["X"] = SymbolState.COOLDOWN
        with pytest.raises(InvalidTransitionError):
            portfolio_manager.transition("X", SymbolState.PENDING_ENTRY)

    def test_active_to_idle_raises(self, portfolio_manager: PortfolioManager) -> None:
        portfolio_manager._symbol_states["X"] = SymbolState.ACTIVE
        with pytest.raises(InvalidTransitionError):
            portfolio_manager.transition("X", SymbolState.IDLE)


# =====================================================================
# Portfolio Manager — Signal Processing
# =====================================================================


class TestPortfolioManagerSignals:
    @pytest.mark.asyncio
    async def test_entry_approved(
        self, portfolio_manager: PortfolioManager, sample_signal: Signal
    ) -> None:
        result = await portfolio_manager.process_signal(sample_signal)
        assert result is True
        assert portfolio_manager.get_symbol_state("RELIANCE") == SymbolState.PENDING_ENTRY

    @pytest.mark.asyncio
    async def test_entry_rejected_when_not_idle(
        self, portfolio_manager: PortfolioManager, sample_signal: Signal
    ) -> None:
        await portfolio_manager.process_signal(sample_signal)
        # Second signal for same symbol while PENDING_ENTRY
        result2 = await portfolio_manager.process_signal(sample_signal)
        assert result2 is False

    @pytest.mark.asyncio
    async def test_exit_without_position_rejected(
        self, portfolio_manager: PortfolioManager, exit_signal: Signal
    ) -> None:
        result = await portfolio_manager.process_signal(exit_signal)
        assert result is False

    @pytest.mark.asyncio
    async def test_exit_after_entry(
        self, portfolio_manager: PortfolioManager, sample_signal: Signal
    ) -> None:
        await portfolio_manager.process_signal(sample_signal)
        # Entry fill transitions IDLE -> PENDING_ENTRY -> ACTIVE
        portfolio_manager.on_order_filled(
            "RELIANCE", sample_signal.quantity, 2500.0, is_entry=True
        )
        exit_sig = Signal(
            symbol="RELIANCE",
            signal_type=SignalType.EXIT_LONG,
            ltp=2550.0,
            rms_approved=False,
        )
        result = await portfolio_manager.process_signal(exit_sig)
        assert result is True
        assert portfolio_manager.get_symbol_state("RELIANCE") == SymbolState.PENDING_EXIT

    @pytest.mark.asyncio
    async def test_sector_limit_rejects(
        self, portfolio_manager: PortfolioManager
    ) -> None:
        ca = portfolio_manager.allocator
        ca._sector_exposure["ENERGY"] = 280_000.0
        # 30% of 1M = 300000, remaining = 20000
        signal = Signal(
            symbol="RELIANCE",
            signal_type=SignalType.BUY,
            ltp=2500.0,
            stop_loss=2450.0,
            target=2600.0,
            rms_approved=False,
        )
        result = await portfolio_manager.process_signal(signal)
        assert result is False


# =====================================================================
# Portfolio Manager — Order Fill + Cooldown
# =====================================================================


class TestPortfolioManagerFills:
    @pytest.mark.asyncio
    async def test_entry_fill_transitions_to_active(
        self, portfolio_manager: PortfolioManager, sample_signal: Signal
    ) -> None:
        await portfolio_manager.process_signal(sample_signal)
        portfolio_manager.on_order_filled("RELIANCE", 10, 2500.0, is_entry=True)
        assert portfolio_manager.get_symbol_state("RELIANCE") == SymbolState.ACTIVE
        pos = portfolio_manager.get_position("RELIANCE")
        assert pos is not None
        assert pos.state == PositionState.ACTIVE
        assert pos.average_price == 2500.0

    @pytest.mark.asyncio
    async def test_exit_fill_transitions_to_cooldown(
        self, portfolio_manager: PortfolioManager, sample_signal: Signal
    ) -> None:
        await portfolio_manager.process_signal(sample_signal)
        portfolio_manager.on_order_filled("RELIANCE", 20, 2500.0, is_entry=True)
        assert portfolio_manager.get_symbol_state("RELIANCE") == SymbolState.ACTIVE
        # Send exit signal then exit fill
        exit_sig = Signal(
            symbol="RELIANCE",
            signal_type=SignalType.EXIT_LONG,
            ltp=2550.0,
        )
        await portfolio_manager.process_signal(exit_sig)
        portfolio_manager.on_order_filled("RELIANCE", 20, 2550.0, is_entry=False)
        assert portfolio_manager.get_symbol_state("RELIANCE") == SymbolState.COOLDOWN
        assert portfolio_manager.get_position("RELIANCE") is None

    def test_cooldown_expiry(self, portfolio_manager: PortfolioManager) -> None:
        portfolio_manager._symbol_states["RELIANCE"] = SymbolState.COOLDOWN
        portfolio_manager._cooldown_until["RELIANCE"] = datetime(2020, 1, 1)
        expired: list[str] = portfolio_manager.tick_cooldown()
        assert "RELIANCE" in expired
        assert portfolio_manager.get_symbol_state("RELIANCE") == SymbolState.IDLE

    def test_cooldown_not_expired(self, portfolio_manager: PortfolioManager) -> None:
        portfolio_manager._symbol_states["RELIANCE"] = SymbolState.COOLDOWN
        portfolio_manager._cooldown_until["RELIANCE"] = datetime(2099, 1, 1)
        expired = portfolio_manager.tick_cooldown()
        assert len(expired) == 0
        assert portfolio_manager.get_symbol_state("RELIANCE") == SymbolState.COOLDOWN


# =====================================================================
# Order Manager — Lifecycle
# =====================================================================


class TestOrderManagerLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_creates_db(self, db_path: str) -> None:
        om = OrderManager(db_path=db_path)
        await om.initialize()
        assert os.path.exists(db_path)

    @pytest.mark.asyncio
    async def test_persists_signal_to_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            om = OrderManager(db_path=db_path, paper_trade=True)
            await om.initialize()

            signal = Signal(
                symbol="RELIANCE",
                signal_type=SignalType.BUY,
                strategy_name="test",
                ltp=2500.0,
                stop_loss=2450.0,
                target=2600.0,
                quantity=10,
                rms_approved=True,
            )
            order = await om.process_signal(signal)
            assert order is not None
            assert order.status == OrderStatus.FILLED
            filled = om.get_orders_by_status(OrderStatus.FILLED)
            assert len(filled) == 1

    @pytest.mark.asyncio
    async def test_unapproved_signal_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            om = OrderManager(db_path=db_path, paper_trade=True)
            await om.initialize()

            signal = Signal(
                symbol="RELIANCE",
                signal_type=SignalType.BUY,
                ltp=2500.0,
                quantity=10,
                rms_approved=False,
            )
            order = await om.process_signal(signal)
            assert order is None

    @pytest.mark.asyncio
    async def test_order_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            om = OrderManager(db_path=db_path, paper_trade=True)
            await om.initialize()

            order = Order(symbol="X", quantity=1)
            om._orders[order.id] = order

            om._transition(order, OrderStatus.SUBMITTED)
            assert order.status == OrderStatus.SUBMITTED

            om._transition(order, OrderStatus.FILLED)
            assert order.status == OrderStatus.FILLED

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            om = OrderManager(db_path=db_path, paper_trade=True)
            await om.initialize()

            order = Order(symbol="X", quantity=1)
            order.status = OrderStatus.FILLED
            om._orders[order.id] = order

            with pytest.raises(ValueError, match="Invalid order transition"):
                om._transition(order, OrderStatus.SUBMITTED)

    @pytest.mark.asyncio
    async def test_cancel_pending_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            om = OrderManager(db_path=db_path, paper_trade=True)
            await om.initialize()

            order = Order(symbol="X", quantity=1)
            order.status = OrderStatus.PENDING
            om._orders[order.id] = order

            result = await om.cancel_order(order.id)
            assert result is True
            assert order.status == OrderStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_filled_order_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            om = OrderManager(db_path=db_path, paper_trade=True)
            await om.initialize()

            order = Order(symbol="X", quantity=1)
            order.status = OrderStatus.FILLED
            om._orders[order.id] = order

            result = await om.cancel_order(order.id)
            assert result is False

    @pytest.mark.asyncio
    async def test_exit_order_records_trade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            om = OrderManager(db_path=db_path, paper_trade=True)
            await om.initialize()

            signal = Signal(
                symbol="RELIANCE",
                signal_type=SignalType.EXIT_LONG,
                strategy_name="test",
                ltp=2550.0,
                quantity=10,
                rms_approved=True,
            )
            order = await om.process_signal(signal)
            assert order is not None
            assert order.status == OrderStatus.FILLED

            trades = await om.get_todays_trades()
            assert len(trades) == 1
            assert trades[0].symbol == "RELIANCE"

    @pytest.mark.asyncio
    async def test_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            om = OrderManager(db_path=db_path, paper_trade=True)
            await om.initialize()

            signal = Signal(
                symbol="RELIANCE",
                signal_type=SignalType.BUY,
                ltp=2500.0,
                quantity=10,
                rms_approved=True,
            )
            await om.process_signal(signal)

            metrics = om.get_metrics()
            assert metrics["orders_placed"] == 1
            assert metrics["orders_filled"] == 1


# =====================================================================
# SQLite Schema
# =====================================================================


class TestDBSchema:
    @pytest.mark.asyncio
    async def test_init_db_creates_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            await init_db(db_path)

            import aiosqlite

            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
                tables = [row[0] for row in await cursor.fetchall()]
                assert "orders" in tables
                assert "positions" in tables
                assert "trades" in tables
                assert "daily_pnl" in tables

    @pytest.mark.asyncio
    async def test_init_db_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            await init_db(db_path)
            await init_db(db_path)  # Should not raise


# =====================================================================
# Integration: Risk + Portfolio + Order Manager
# =====================================================================


class TestIntegration:
    @pytest.mark.asyncio
    async def test_full_flow_entry_and_exit(self) -> None:
        """End-to-end: signal -> portfolio -> risk -> order -> fill."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")

            # Setup
            ca = CapitalAllocator(total_capital=1_000_000, max_active_trades=3)
            pm = PortfolioManager(
                capital_allocator=ca,
                sector_map={"ENERGY": ["RELIANCE"]},
            )
            rm = RiskManager()
            rm.set_capital(1_000_000.0)
            om = OrderManager(db_path=db_path, paper_trade=True)
            await om.initialize()

            # Wire: PM fill callback -> OM
            pm._position_update_callbacks.append(
                lambda pos: None  # no-op for this test
            )

            # 1. Create signal
            signal = Signal(
                symbol="RELIANCE",
                signal_type=SignalType.BUY,
                strategy_name="test",
                ltp=2500.0,
                stop_loss=2450.0,
                target=2600.0,
                rms_approved=False,
            )

            # 2. Portfolio Manager processes
            approved = await pm.process_signal(signal)
            assert approved is True

            # 3. Risk Manager validates
            ps = pm.get_portfolio_state()
            risk_ok, reason = rm.validate_signal(
                signal,
                current_exposure=ps.capital.used_margin,
                open_position_count=ps.active_positions,
            )
            assert risk_ok is True
            signal.rms_approved = True

            # 4. Order Manager executes
            order = await om.process_signal(signal)
            assert order is not None
            assert order.status == OrderStatus.FILLED

            # 5. Notify fill back to PM
            pm.on_order_filled("RELIANCE", signal.quantity, 2500.0, is_entry=True)
            assert pm.get_symbol_state("RELIANCE") == SymbolState.ACTIVE

            # 6. Exit signal
            exit_signal = Signal(
                symbol="RELIANCE",
                signal_type=SignalType.EXIT_LONG,
                strategy_name="test",
                ltp=2550.0,
                rms_approved=True,
            )
            exit_approved = await pm.process_signal(exit_signal)
            assert exit_approved is True

            exit_order = await om.process_signal(exit_signal)
            assert exit_order is not None
            assert exit_order.status == OrderStatus.FILLED

            # 7. Notify exit fill
            pm.on_order_filled("RELIANCE", exit_signal.quantity, 2550.0, is_entry=False)
            assert pm.get_symbol_state("RELIANCE") == SymbolState.COOLDOWN

    @pytest.mark.asyncio
    async def test_risk_rejects_over_exposure(self) -> None:
        ca = CapitalAllocator(total_capital=1_000_000, max_active_trades=3)
        pm = PortfolioManager(capital_allocator=ca)
        rm = RiskManager()
        rm.set_capital(1_000_000.0)

        # qty=10 at 2500 = 25000 order value (under max_order_value 50000)
        # 50% exposure cap = 500000
        signal = Signal(
            symbol="RELIANCE",
            signal_type=SignalType.BUY,
            ltp=2500.0,
            stop_loss=2450.0,
            target=2600.0,
            quantity=10,
            rms_approved=False,
        )
        # exposure 490000 + 25000 = 515000 > 500000 -> reject
        approved, reason = rm.validate_signal(signal, 490_000.0, 2)
        assert approved is False
        assert "exposure" in reason.lower()
