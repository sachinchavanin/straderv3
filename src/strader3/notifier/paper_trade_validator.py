"""Paper-trade validator — logs signals to SQLite and tracks hypothetical P&L.

Every signal (entry/exit) is persisted with timestamp, price, and direction.
Hypothetical P&L is computed from entry to exit using actual price movement.
Daily summary reports include win rate, average P&L, and max drawdown.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import aiosqlite
import structlog

from strader3.models.trading import Signal, SignalType

logger = structlog.get_logger(__name__)

# Schema version for paper-trade tables
_PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_signals (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    price REAL NOT NULL,
    quantity INTEGER DEFAULT 0,
    stop_loss REAL DEFAULT 0.0,
    target REAL DEFAULT 0.0,
    direction TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    strategy_name TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    indicators TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL DEFAULT 0.0,
    quantity INTEGER NOT NULL,
    entry_time TIMESTAMP NOT NULL,
    exit_time TIMESTAMP,
    gross_pnl REAL DEFAULT 0.0,
    status TEXT DEFAULT 'OPEN',
    exit_reason TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS paper_daily_summary (
    date TEXT PRIMARY KEY,
    total_pnl REAL DEFAULT 0.0,
    trades_count INTEGER DEFAULT 0,
    wins_count INTEGER DEFAULT 0,
    losses_count INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0.0,
    avg_pnl REAL DEFAULT 0.0,
    max_drawdown REAL DEFAULT 0.0,
    avg_win REAL DEFAULT 0.0,
    avg_loss REAL DEFAULT 0.0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_paper_signals_symbol ON paper_signals(symbol);
CREATE INDEX IF NOT EXISTS idx_paper_signals_timestamp ON paper_signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol ON paper_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
"""


@dataclass(slots=True)
class PaperTradeRecord:
    """Represents a completed paper trade for reporting."""

    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    quantity: int
    entry_time: datetime
    exit_time: datetime
    gross_pnl: float
    exit_reason: str = ""


@dataclass(slots=True)
class DailyReport:
    """Daily paper-trade summary report."""

    report_date: str
    total_pnl: float
    trades_count: int
    wins_count: int
    losses_count: int
    win_rate: float
    avg_pnl: float
    max_drawdown: float
    avg_win: float
    avg_loss: float
    trades: list[PaperTradeRecord] = field(default_factory=list)


class PaperTradeValidator:
    """Logs signals to SQLite and tracks hypothetical P&L.

    Features:
        - Every signal logged with timestamp, price, direction
        - Hypothetical P&L: entry -> exit based on actual price movement
        - Daily summary: win rate, avg P&L, max drawdown
        - Separate paper-trade tables (no interference with live tables)
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._open_trades: dict[str, dict[str, Any]] = {}
        self._initialized = False

    async def _ensure_db(self) -> None:
        """Ensure paper-trade tables exist."""
        if self._initialized:
            return
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_PAPER_SCHEMA)
            await db.commit()
        self._initialized = True

    async def log_signal(self, signal: Signal) -> str:
        """Log a trading signal to the paper_signals table.

        Returns the signal record ID.
        """
        await self._ensure_db()

        direction = "LONG" if signal.signal_type == SignalType.BUY else (
            "SHORT" if signal.signal_type == SignalType.SELL else "EXIT"
        )

        signal_id = f"psig_{signal.id}"

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO paper_signals
                (id, symbol, signal_type, price, quantity, stop_loss, target,
                 direction, timestamp, strategy_name, reason, indicators)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    signal.symbol,
                    signal.signal_type.value,
                    signal.ltp,
                    signal.quantity,
                    signal.stop_loss,
                    signal.target,
                    direction,
                    signal.timestamp.isoformat(),
                    signal.strategy_name,
                    signal.reason,
                    json.dumps(signal.indicators),
                ),
            )
            await db.commit()

        logger.debug(
            "paper_validator.signal_logged",
            signal_id=signal_id,
            symbol=signal.symbol,
            direction=direction,
            price=signal.ltp,
        )

        # Track open trades for P&L computation
        if signal.signal_type in (SignalType.BUY, SignalType.SELL):
            self._open_trades[signal.symbol] = {
                "direction": direction,
                "entry_price": signal.ltp,
                "quantity": signal.quantity,
                "entry_time": signal.timestamp,
                "stop_loss": signal.stop_loss,
                "target": signal.target,
            }
        elif signal.signal_type in (SignalType.EXIT_LONG, SignalType.EXIT_SHORT):
            if signal.symbol in self._open_trades:
                await self._close_trade(signal)

        return signal_id

    async def _close_trade(self, signal: Signal) -> float:
        """Close an open paper trade and compute P&L. Returns gross P&L."""
        open_trade = self._open_trades.pop(signal.symbol)
        entry_price = open_trade["entry_price"]
        exit_price = signal.ltp
        quantity = open_trade["quantity"]
        direction = open_trade["direction"]

        if direction == "LONG":
            gross_pnl = (exit_price - entry_price) * quantity
        else:
            gross_pnl = (entry_price - exit_price) * quantity

        trade_id = f"pt_{signal.id}"

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO paper_trades
                (id, symbol, direction, entry_price, exit_price, quantity,
                 entry_time, exit_time, gross_pnl, status, exit_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    signal.symbol,
                    direction,
                    entry_price,
                    exit_price,
                    quantity,
                    open_trade["entry_time"].isoformat(),
                    signal.timestamp.isoformat(),
                    gross_pnl,
                    "CLOSED",
                    signal.reason or "signal_exit",
                ),
            )
            await db.commit()

        logger.info(
            "paper_validator.trade_closed",
            symbol=signal.symbol,
            direction=direction,
            entry=entry_price,
            exit=exit_price,
            pnl=gross_pnl,
        )

        return gross_pnl

    async def generate_daily_report(self, report_date: str | None = None) -> DailyReport:
        """Generate a daily paper-trade summary report.

        Args:
            report_date: ISO date string (YYYY-MM-DD). Defaults to today.

        Returns:
            DailyReport with win rate, avg P&L, max drawdown, etc.
        """
        await self._ensure_db()

        if report_date is None:
            report_date = date.today().isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Get all closed trades for the date
            async with db.execute(
                """
                SELECT * FROM paper_trades
                WHERE date(exit_time) = ?
                ORDER BY exit_time ASC
                """,
                (report_date,),
            ) as cursor:
                rows = await cursor.fetchall()

            trades = [
                PaperTradeRecord(
                    symbol=r["symbol"],
                    direction=r["direction"],
                    entry_price=r["entry_price"],
                    exit_price=r["exit_price"],
                    quantity=r["quantity"],
                    entry_time=datetime.fromisoformat(r["entry_time"]),
                    exit_time=datetime.fromisoformat(r["exit_time"]),
                    gross_pnl=r["gross_pnl"],
                    exit_reason=r["exit_reason"],
                )
                for r in rows
            ]

        if not trades:
            return DailyReport(
                report_date=report_date,
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

        pnls = [t.gross_pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        total_pnl = sum(pnls)

        # Max drawdown: worst cumulative P&L from peak
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        report = DailyReport(
            report_date=report_date,
            total_pnl=total_pnl,
            trades_count=len(trades),
            wins_count=len(wins),
            losses_count=len(losses),
            win_rate=len(wins) / len(trades) if trades else 0.0,
            avg_pnl=total_pnl / len(trades) if trades else 0.0,
            max_drawdown=max_dd,
            avg_win=sum(wins) / len(wins) if wins else 0.0,
            avg_loss=sum(losses) / len(losses) if losses else 0.0,
            trades=trades,
        )

        # Persist summary
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO paper_daily_summary
                (date, total_pnl, trades_count, wins_count, losses_count,
                 win_rate, avg_pnl, max_drawdown, avg_win, avg_loss, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    total_pnl = excluded.total_pnl,
                    trades_count = excluded.trades_count,
                    wins_count = excluded.wins_count,
                    losses_count = excluded.losses_count,
                    win_rate = excluded.win_rate,
                    avg_pnl = excluded.avg_pnl,
                    max_drawdown = excluded.max_drawdown,
                    avg_win = excluded.avg_win,
                    avg_loss = excluded.avg_loss,
                    updated_at = excluded.updated_at
                """,
                (
                    report_date,
                    report.total_pnl,
                    report.trades_count,
                    report.wins_count,
                    report.losses_count,
                    report.win_rate,
                    report.avg_pnl,
                    report.max_drawdown,
                    report.avg_win,
                    report.avg_loss,
                    datetime.now().isoformat(),
                ),
            )
            await db.commit()

        logger.info(
            "paper_validator.daily_report",
            date=report_date,
            trades=report.trades_count,
            win_rate=f"{report.win_rate:.1%}",
            total_pnl=report.total_pnl,
        )

        return report

    async def get_open_trades(self) -> list[dict[str, Any]]:
        """Get all currently open paper trades."""
        return [
            {"symbol": sym, **data}
            for sym, data in self._open_trades.items()
        ]

    async def get_trade_history(
        self, symbol: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Get completed paper trade history."""
        await self._ensure_db()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if symbol:
                async with db.execute(
                    "SELECT * FROM paper_trades WHERE symbol = ? ORDER BY exit_time DESC LIMIT ?",
                    (symbol, limit),
                ) as cursor:
                    rows = await cursor.fetchall()
            else:
                async with db.execute(
                    "SELECT * FROM paper_trades ORDER BY exit_time DESC LIMIT ?",
                    (limit,),
                ) as cursor:
                    rows = await cursor.fetchall()

            return [dict(r) for r in rows]

    def format_report_text(self, report: DailyReport) -> str:
        """Format a DailyReport as human-readable text."""
        lines = [
            f"=== Paper Trade Report: {report.report_date} ===",
            f"Total P&L:      Rs.{report.total_pnl:+,.2f}",
            f"Trades:         {report.trades_count}",
            f"Win Rate:       {report.win_rate:.1%} ({report.wins_count}W / {report.losses_count}L)",
            f"Avg P&L:        Rs.{report.avg_pnl:+,.2f}",
            f"Avg Win:        Rs.{report.avg_win:+,.2f}",
            f"Avg Loss:       Rs.{report.avg_loss:+,.2f}",
            f"Max Drawdown:   Rs.{report.max_drawdown:,.2f}",
        ]

        if report.trades:
            lines.append("")
            lines.append("--- Trade Details ---")
            for t in report.trades:
                sign = "+" if t.gross_pnl >= 0 else ""
                lines.append(
                    f"  {t.symbol:15s} {t.direction:5s} "
                    f"Entry: Rs.{t.entry_price:,.2f} -> Exit: Rs.{t.exit_price:,.2f} "
                    f"P&L: Rs.{sign}{t.gross_pnl:,.2f} ({t.exit_reason})"
                )

        return "\n".join(lines)
