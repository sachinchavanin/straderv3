"""Repository pattern — SQLite persistence for all trading entities.

Provides a repository per entity (signals, positions, trades, orders, daily_pnl)
and a migration framework with schema version tracking.
"""

import json
from datetime import datetime
from typing import Any

import aiosqlite
import structlog

from strader3.core.db import init_db
from strader3.models.trading import Order, Position, Signal, Trade

logger = structlog.get_logger(__name__)

# Current schema version — bump on schema changes
_SCHEMA_VERSION = 1


class Repository:
    """Base repository with common CRUD operations."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def _ensure_initialized(self) -> None:
        """Ensure DB is initialized with current schema and migrations applied."""
        await init_db(self.db_path)
        await MigrationManager(self.db_path).migrate()

    async def execute(self, query: str, params: tuple = ()) -> aiosqlite.Cursor:
        """Execute a query and return cursor."""
        await self._ensure_initialized()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor

    async def fetchone(self, query: str, params: tuple = ()) -> Any | None:
        """Fetch a single row."""
        await self._ensure_initialized()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                return await cursor.fetchone()

    async def fetchall(self, query: str, params: tuple = ()) -> list[Any]:
        """Fetch all matching rows."""
        await self._ensure_initialized()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return rows


class SignalRepository(Repository):
    """Persistence for trading signals."""

    async def save(self, signal: Signal) -> None:
        """Persist a signal to the database."""
        await self.execute(
            """
            INSERT OR REPLACE INTO signals
            (id, symbol, signal_type, strategy_name, ltp, stop_loss, target,
             max_holding_minutes, timestamp, reason, indicators, quantity,
             allocated_capital, rms_approved, rms_rejection_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.id,
                signal.symbol,
                signal.signal_type.value,
                signal.strategy_name,
                signal.ltp,
                signal.stop_loss,
                signal.target,
                signal.max_holding_minutes,
                signal.timestamp.isoformat() if signal.timestamp else None,
                signal.reason,
                json.dumps(signal.indicators),
                signal.quantity,
                signal.allocated_capital,
                signal.rms_approved,
                signal.rms_rejection_reason,
            ),
        )
        logger.debug("signal_repo.saved", signal_id=signal.id, symbol=signal.symbol)

    async def get_by_id(self, signal_id: str) -> Signal | None:
        """Retrieve a signal by ID."""
        row = await self.fetchone(
            "SELECT * FROM signals WHERE id = ?", (signal_id,)
        )
        if not row:
            return None
        return Signal(
            id=row["id"],
            symbol=row["symbol"],
            signal_type=__import__("strader3.models.trading", fromlist=["SignalType"]).SignalType(row["signal_type"]),
            strategy_name=row["strategy_name"],
            ltp=row["ltp"],
            stop_loss=row["stop_loss"],
            target=row["target"],
            max_holding_minutes=row["max_holding_minutes"],
            timestamp=(
                datetime.fromisoformat(row["timestamp"]) if row["timestamp"] else None
            ),
            reason=row["reason"],
            indicators=json.loads(row["indicators"]) if row["indicators"] else {},
            quantity=row["quantity"],
            allocated_capital=row["allocated_capital"],
            rms_approved=bool(row["rms_approved"]),
            rms_rejection_reason=row["rms_rejection_reason"],
        )

    async def get_by_symbol(self, symbol: str, limit: int = 50) -> list[dict]:
        """Get recent signals for a symbol."""
        rows = await self.fetchall(
            "SELECT * FROM signals WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
            (symbol, limit),
        )
        return [dict(r) for r in rows]

    async def get_all(self, limit: int = 100) -> list[dict]:
        """Get all signals ordered by timestamp desc."""
        rows = await self.fetchall(
            "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]


class PositionRepository(Repository):
    """Persistence for positions."""

    async def save(self, position: Position) -> None:
        """Persist a position."""
        await self.execute(
            """
            INSERT OR REPLACE INTO positions
            (id, symbol, state, quantity, average_price, current_price,
             entry_order_id, entry_time, entry_signal_id, strategy_name,
             stop_loss, target, trailing_stop, max_holding_until,
             realized_pnl, unrealized_pnl, sector, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"pos_{position.symbol}",
                position.symbol,
                position.state.value,
                position.quantity,
                position.average_price,
                position.current_price,
                position.entry_order_id,
                position.entry_time.isoformat() if position.entry_time else None,
                position.entry_signal_id,
                position.strategy_name,
                position.stop_loss,
                position.target,
                position.trailing_stop,
                position.max_holding_until.isoformat() if position.max_holding_until else None,
                position.realized_pnl,
                position.unrealized_pnl,
                position.sector,
                datetime.now().isoformat(),
            ),
        )
        logger.debug("position_repo.saved", symbol=position.symbol, state=position.state.value)

    async def get_by_symbol(self, symbol: str) -> dict | None:
        """Get position by symbol."""
        row = await self.fetchone(
            "SELECT * FROM positions WHERE symbol = ?", (symbol,)
        )
        if not row:
            return None
        return dict(row)

    async def get_all(self) -> list[dict]:
        """Get all positions."""
        rows = await self.fetchall("SELECT * FROM positions ORDER BY symbol")
        return [dict(r) for r in rows]

    async def delete(self, symbol: str) -> None:
        """Remove a position record."""
        await self.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        logger.debug("position_repo.deleted", symbol=symbol)


class TradeRepository(Repository):
    """Persistence for completed trades."""

    async def save(self, trade: Trade) -> None:
        """Persist a completed trade."""
        await self.execute(
            """
            INSERT OR REPLACE INTO trades
            (id, symbol, strategy_name, entry_time, entry_price, entry_quantity,
             entry_order_id, exit_time, exit_price, exit_quantity, exit_order_id,
             exit_reason, gross_pnl, charges, net_pnl, signal_price,
             entry_slippage, exit_slippage, execution_delay_ms, signal_id, sector)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.id,
                trade.symbol,
                trade.strategy_name,
                trade.entry_time.isoformat() if trade.entry_time else None,
                trade.entry_price,
                trade.entry_quantity,
                trade.entry_order_id,
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
        logger.debug("trade_repo.saved", trade_id=trade.id, symbol=trade.symbol)

    async def get_by_symbol(self, symbol: str, limit: int = 50) -> list[dict]:
        """Get trades for a symbol."""
        rows = await self.fetchall(
            "SELECT * FROM trades WHERE symbol = ? ORDER BY exit_time DESC LIMIT ?",
            (symbol, limit),
        )
        return [dict(r) for r in rows]

    async def get_todays_trades(self) -> list[dict]:
        """Get today's trades."""
        today = datetime.now().date().isoformat()
        rows = await self.fetchall(
            "SELECT * FROM trades WHERE date(exit_time) = ? ORDER BY exit_time DESC",
            (today,),
        )
        return [dict(r) for r in rows]

    async def get_pnl_summary(self) -> dict:
        """Get P&L summary for trade log export."""
        row = await self.fetchone(
            """
            SELECT
                COUNT(*) as total_trades,
                COALESCE(SUM(gross_pnl), 0) as total_gross_pnl,
                COALESCE(SUM(charges), 0) as total_charges,
                COALESCE(SUM(net_pnl), 0) as total_net_pnl,
                COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END), 0) as wins,
                COALESCE(SUM(CASE WHEN net_pnl < 0 THEN 1 ELSE 0 END), 0) as losses
            FROM trades
            """
        )
        if not row:
            return {
                "total_trades": 0,
                "total_gross_pnl": 0.0,
                "total_charges": 0.0,
                "total_net_pnl": 0.0,
                "wins": 0,
                "losses": 0,
            }
        return dict(row)


class DailyPnlRepository(Repository):
    """Persistence for daily P&L tracking."""

    async def save_daily_pnl(
        self,
        date: str,
        realized_pnl: float,
        unrealized_pnl: float,
        trades_count: int = 0,
        wins_count: int = 0,
        losses_count: int = 0,
        charges: float = 0.0,
    ) -> None:
        """Save or update daily P&L record."""
        total = realized_pnl + unrealized_pnl
        await self.execute(
            """
            INSERT INTO daily_pnl
            (date, realized_pnl, unrealized_pnl, total_pnl, trades_count,
             wins_count, losses_count, charges, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                realized_pnl = excluded.realized_pnl,
                unrealized_pnl = excluded.unrealized_pnl,
                total_pnl = excluded.total_pnl,
                trades_count = excluded.trades_count,
                wins_count = excluded.wins_count,
                losses_count = excluded.losses_count,
                charges = excluded.charges,
                updated_at = excluded.updated_at
            """,
            (
                date,
                realized_pnl,
                unrealized_pnl,
                total,
                trades_count,
                wins_count,
                losses_count,
                charges,
                datetime.now().isoformat(),
            ),
        )
        logger.debug("daily_pnl_repo.saved", date=date, total_pnl=total)

    async def get_by_date(self, date: str) -> dict | None:
        """Get P&L for a specific date."""
        row = await self.fetchone(
            "SELECT * FROM daily_pnl WHERE date = ?", (date,)
        )
        if not row:
            return None
        return dict(row)

    async def get_all(self, limit: int = 30) -> list[dict]:
        """Get recent daily P&L records."""
        rows = await self.fetchall(
            "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]


class OrderRepository(Repository):
    """Persistence for orders (delegates to OrderManager's persistence)."""

    async def save(self, order: Order) -> None:
        """Persist an order."""
        await self.execute(
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

    async def get_by_id(self, order_id: str) -> dict | None:
        """Get order by ID."""
        row = await self.fetchone(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        )
        if not row:
            return None
        return dict(row)

    async def get_by_status(self, status: str) -> list[dict]:
        """Get orders by status."""
        rows = await self.fetchall(
            "SELECT * FROM orders WHERE status = ? ORDER BY created_at DESC",
            (status,),
        )
        return [dict(r) for r in rows]

    async def get_all(self, limit: int = 100) -> list[dict]:
        """Get all orders."""
        rows = await self.fetchall(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]


class MigrationManager:
    """Handles database migration versioning."""

    MIGRATIONS = {
        1: """
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                signal_type TEXT,
                strategy_name TEXT,
                ltp REAL,
                stop_loss REAL,
                target REAL,
                max_holding_minutes INTEGER,
                timestamp TIMESTAMP,
                reason TEXT,
                indicators TEXT,
                quantity INTEGER DEFAULT 0,
                allocated_capital REAL DEFAULT 0.0,
                rms_approved INTEGER DEFAULT 0,
                rms_rejection_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
            CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
            CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy_name);

            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """,
    }

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def migrate(self) -> None:
        """Run all pending migrations."""
        await init_db(self.db_path)

        current_version = await self._get_current_version()
        target_version = max(self.MIGRATIONS.keys())

        if current_version >= target_version:
            logger.debug("db.migrations_up_to_date", version=current_version)
            return

        for version in range(current_version + 1, target_version + 1):
            if version in self.MIGRATIONS:
                async with aiosqlite.connect(self.db_path) as db:
                    await db.executescript(self.MIGRATIONS[version])
                    await db.execute(
                        "INSERT OR REPLACE INTO schema_migrations (version) VALUES (?)",
                        (version,),
                    )
                    await db.commit()
                logger.info("db.migration_applied", version=version)

        logger.info("db.migrations_complete", from_version=current_version, to_version=target_version)

    async def _get_current_version(self) -> int:
        """Get current schema version."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT MAX(version) as ver FROM schema_migrations"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row["ver"] is not None:
                        return row["ver"]
        except Exception:
            pass
        return 0
