"""Order Management System — order execution and lifecycle management.

Handles order placement, state tracking, SQLite persistence,
and simulated fills for paper-trade mode.
"""

import asyncio
import os
import sqlite3
from datetime import datetime
from typing import Callable, Optional
from uuid import uuid4

import aiosqlite
import structlog

from strader3.core.db import init_db
from strader3.models.trading import (
    Order,
    OrderStatus,
    OrderType,
    ProductType,
    Signal,
    SignalType,
    Trade,
)

logger = structlog.get_logger(__name__)


class OrderManager:
    """Manages order lifecycle and SQLite persistence.

    Order lifecycle:
        PENDING -> SUBMITTED -> FILLED
                            -> CANCELLED
                            -> REJECTED

    Phase 1: paper-trade mode with simulated fills (no real broker).
    """

    VALID_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
        OrderStatus.PENDING: {
            OrderStatus.SUBMITTED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        },
        OrderStatus.SUBMITTED: {
            OrderStatus.FILLED,
            OrderStatus.PARTIAL,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        },
        OrderStatus.PARTIAL: {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        },
        OrderStatus.FILLED: set(),
        OrderStatus.CANCELLED: set(),
        OrderStatus.REJECTED: set(),
        OrderStatus.EXPIRED: set(),
        OrderStatus.ERROR: set(),
    }

    def __init__(
        self,
        db_path: str,
        product_type: ProductType = ProductType.MIS,
        paper_trade: bool = True,
    ) -> None:
        self.db_path = db_path
        self.product_type = product_type
        self.paper_trade = paper_trade

        self._orders: dict[str, Order] = {}
        self._db_initialized = False

        # Callbacks
        self._fill_callbacks: list[Callable[[str, int, float, bool], None]] = []

        # Metrics
        self._orders_placed: int = 0
        self._orders_filled: int = 0
        self._orders_rejected: int = 0
        self._orders_cancelled: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize order manager and database."""
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        await init_db(self.db_path)
        self._db_initialized = True
        logger.info("order_manager.initialized", db_path=self.db_path)

    # ------------------------------------------------------------------
    # Order state machine
    # ------------------------------------------------------------------

    def _transition(self, order: Order, new_status: OrderStatus) -> None:
        """Transition order to a new status.

        Raises:
            ValueError: If the transition is invalid.
        """
        current = order.status
        allowed = self.VALID_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            raise ValueError(
                f"Invalid order transition: {current.value} -> {new_status.value} "
                f"(order {order.id})"
            )
        order.status = new_status
        logger.debug(
            "order_manager.transition",
            order_id=order.id,
            old=current.value,
            new=new_status.value,
        )

    # ------------------------------------------------------------------
    # Signal processing
    # ------------------------------------------------------------------

    async def process_signal(self, signal: Signal) -> Optional[Order]:
        """Process RMS-approved signal into an order.

        Returns:
            Order object or None if signal is not approved.
        """
        if not signal.rms_approved:
            logger.warning(
                "order_manager.signal_not_approved",
                signal_id=signal.id,
            )
            return None

        order = Order(
            symbol=signal.symbol,
            side=signal.signal_type,
            order_type=OrderType.MARKET,
            product_type=self.product_type,
            quantity=signal.quantity,
            signal_id=signal.id,
        )

        self._orders[order.id] = order
        await self._persist_order(order)

        try:
            result = await self._execute(order, signal)
            return result
        except Exception:
            logger.exception("order_manager.execution_error", order_id=order.id)
            self._transition(order, OrderStatus.ERROR)
            order.rejection_reason = "Execution error"
            await self._persist_order(order)
            return order

    async def _execute(self, order: Order, signal: Signal) -> Order:
        """Execute an order (paper-trade or live)."""
        self._transition(order, OrderStatus.SUBMITTED)
        order.submitted_at = datetime.now()
        self._orders_placed += 1

        if self.paper_trade:
            result = await self._simulated_fill(order, signal)
        else:
            # Live broker integration — Phase 2
            raise NotImplementedError("Live broker integration not yet implemented")

        await self._persist_order(result)

        if result.status == OrderStatus.FILLED:
            await self._handle_fill(result, signal)
        elif result.status == OrderStatus.REJECTED:
            self._orders_rejected += 1
            logger.warning(
                "order_manager.order_rejected",
                order_id=order.id,
                reason=result.rejection_reason,
            )

        return result

    async def _simulated_fill(self, order: Order, signal: Signal) -> Order:
        """Simulate an immediate fill at signal price (paper-trade)."""
        # Simulate minimal delay
        await asyncio.sleep(0)

        order.filled_quantity = order.quantity
        order.average_price = signal.ltp
        order.filled_at = datetime.now()
        self._transition(order, OrderStatus.FILLED)
        self._orders_filled += 1

        logger.info(
            "order_manager.simulated_fill",
            order_id=order.id,
            symbol=order.symbol,
            quantity=order.filled_quantity,
            price=order.average_price,
        )
        return order

    async def _handle_fill(self, order: Order, signal: Signal) -> None:
        """Handle order fill — notify callbacks and record trade."""
        is_entry = signal.signal_type in (SignalType.BUY, SignalType.SELL)

        # Notify fill callbacks
        for cb in self._fill_callbacks:
            try:
                cb(order.symbol, order.filled_quantity, order.average_price, is_entry)
            except Exception:
                logger.exception("order_manager.fill_callback_error")

        # If exit, record completed trade
        if not is_entry:
            await self._record_trade(order, signal)

    async def _record_trade(self, exit_order: Order, signal: Signal) -> None:
        """Record a completed trade to the database."""
        trade = Trade(
            symbol=exit_order.symbol,
            strategy_name=signal.strategy_name,
            exit_time=exit_order.filled_at,
            exit_price=exit_order.average_price,
            exit_quantity=exit_order.filled_quantity,
            exit_order_id=exit_order.id,
            exit_reason=signal.reason or "normal",
            signal_id=signal.id,
        )
        await self._persist_trade(trade)
        logger.info(
            "order_manager.trade_recorded",
            trade_id=trade.id,
            symbol=trade.symbol,
        )

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending/submitted order.

        Returns:
            True if cancellation succeeded.
        """
        order = self._orders.get(order_id)
        if not order:
            logger.warning("order_manager.cancel_not_found", order_id=order_id)
            return False

        try:
            self._transition(order, OrderStatus.CANCELLED)
        except ValueError:
            logger.warning(
                "order_manager.cancel_invalid_transition",
                order_id=order_id,
                status=order.status.value,
            )
            return False

        order.cancelled_at = datetime.now()
        self._orders_cancelled += 1
        await self._persist_order(order)
        logger.info("order_manager.order_cancelled", order_id=order_id)
        return True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist_order(self, order: Order) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO orders
                (id, client_order_id, symbol, side, order_type, product_type,
                 quantity, filled_quantity, price, trigger_price, status,
                 average_price, created_at, submitted_at, filled_at, cancelled_at,
                 signal_id, rejection_reason, broker_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.id,
                    order.client_order_id,
                    order.symbol,
                    order.side.value,
                    order.order_type.value,
                    order.product_type.value,
                    order.quantity,
                    order.filled_quantity,
                    order.price,
                    order.trigger_price,
                    order.status.value,
                    order.average_price,
                    order.created_at.isoformat() if order.created_at else None,
                    order.submitted_at.isoformat() if order.submitted_at else None,
                    order.filled_at.isoformat() if order.filled_at else None,
                    order.cancelled_at.isoformat() if order.cancelled_at else None,
                    order.signal_id,
                    order.rejection_reason,
                    order.broker_message,
                ),
            )
            await db.commit()

    async def _persist_trade(self, trade: Trade) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO trades
                (id, symbol, strategy_name, exit_time, exit_price, exit_quantity,
                 exit_order_id, exit_reason, gross_pnl, charges, net_pnl,
                 signal_price, entry_slippage, exit_slippage, execution_delay_ms,
                 signal_id, sector)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.id,
                    trade.symbol,
                    trade.strategy_name,
                    trade.exit_time.isoformat() if trade.exit_time else None,
                    trade.exit_price,
                    trade.exit_quantity,
                    trade.exit_order_id,
                    trade.exit_reason,
                    trade.gross_pnl,
                    trade.charges,
                    trade.net_pnl,
                    trade.signal_price,
                    trade.entry_slippage,
                    trade.exit_slippage,
                    trade.execution_delay_ms,
                    trade.signal_id,
                    trade.sector,
                ),
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def get_orders_by_status(self, status: OrderStatus) -> list[Order]:
        return [o for o in self._orders.values() if o.status == status]

    def get_all_orders(self) -> list[Order]:
        return list(self._orders.values())

    async def get_todays_trades(self) -> list[Trade]:
        today = datetime.now().date().isoformat()
        trades: list[Trade] = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM trades
                WHERE date(exit_time) = ?
                ORDER BY exit_time DESC
                """,
                (today,),
            ) as cursor:
                async for row in cursor:
                    trades.append(
                        Trade(
                            id=row["id"],
                            symbol=row["symbol"],
                            strategy_name=row["strategy_name"],
                            exit_time=(
                                datetime.fromisoformat(row["exit_time"])
                                if row["exit_time"]
                                else None
                            ),
                            exit_price=row["exit_price"] or 0.0,
                            exit_quantity=row["exit_quantity"] or 0,
                            exit_order_id=row["exit_order_id"] or "",
                            exit_reason=row["exit_reason"] or "",
                            gross_pnl=row["gross_pnl"] or 0.0,
                            net_pnl=row["net_pnl"] or 0.0,
                        )
                    )
        return trades

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def get_metrics(self) -> dict:
        return {
            "orders_placed": self._orders_placed,
            "orders_filled": self._orders_filled,
            "orders_rejected": self._orders_rejected,
            "orders_cancelled": self._orders_cancelled,
            "total_orders_tracked": len(self._orders),
        }
