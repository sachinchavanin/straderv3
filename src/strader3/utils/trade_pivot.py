"""Trade pivot CSV export — flatten trade records for analysis.

Exports trades to a CSV file suitable for pivot table analysis in Excel/Google Sheets.
Each row is one trade with all key metrics flattened.
"""

import csv
from datetime import datetime
from pathlib import Path

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)

DEFAULT_CSV_COLUMNS = [
    "trade_id",
    "symbol",
    "sector",
    "strategy_name",
    "entry_time",
    "entry_price",
    "entry_quantity",
    "exit_time",
    "exit_price",
    "exit_quantity",
    "exit_reason",
    "gross_pnl",
    "charges",
    "net_pnl",
    "signal_price",
    "entry_slippage",
    "exit_slippage",
    "execution_delay_ms",
    "holding_minutes",
]


async def export_trades_csv(
    db_path: str,
    output_path: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Export all trades from SQLite to a CSV file.

    Args:
        db_path: Path to the SQLite database.
        output_path: Path for the output CSV file.
        start_date: Optional filter (ISO date string, inclusive).
        end_date: Optional filter (ISO date string, inclusive).

    Returns:
        Absolute path to the written CSV file.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    query = "SELECT * FROM trades WHERE 1=1"
    params: list = []

    if start_date:
        query += " AND date(exit_time) >= ?"
        params.append(start_date)
    if end_date:
        query += " AND date(exit_time) <= ?"
        params.append(end_date)

    query += " ORDER BY exit_time ASC"

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        logger.warning("trade_pivot_csv.no_trades", db_path=db_path)
        # Write empty CSV with headers
        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(DEFAULT_CSV_COLUMNS)
        return str(Path(output_path).resolve())

    # Build rows
    csv_rows = []
    for row in rows:
        entry_time = row["entry_time"]
        exit_time = row["exit_time"]

        # Calculate holding minutes
        holding_minutes = ""
        if entry_time and exit_time:
            try:
                entry_dt = datetime.fromisoformat(entry_time)
                exit_dt = datetime.fromisoformat(exit_time)
                holding_minutes = str(
                    int((exit_dt - entry_dt).total_seconds() / 60)
                )
            except (ValueError, TypeError):
                pass

        csv_rows.append(
            {
                "trade_id": row["id"],
                "symbol": row["symbol"],
                "sector": row["sector"] or "",
                "strategy_name": row["strategy_name"] or "",
                "entry_time": entry_time or "",
                "entry_price": row["entry_price"] or 0.0,
                "entry_quantity": row["entry_quantity"] or 0,
                "exit_time": exit_time or "",
                "exit_price": row["exit_price"] or 0.0,
                "exit_quantity": row["exit_quantity"] or 0,
                "exit_reason": row["exit_reason"] or "",
                "gross_pnl": row["gross_pnl"] or 0.0,
                "charges": row["charges"] or 0.0,
                "net_pnl": row["net_pnl"] or 0.0,
                "signal_price": row["signal_price"] or 0.0,
                "entry_slippage": row["entry_slippage"] or 0.0,
                "exit_slippage": row["exit_slippage"] or 0.0,
                "execution_delay_ms": row["execution_delay_ms"] or 0,
                "holding_minutes": holding_minutes,
            }
        )

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DEFAULT_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(csv_rows)

    abs_path = str(Path(output_path).resolve())
    logger.info(
        "trade_pivot_csv.exported",
        path=abs_path,
        rows=len(csv_rows),
        start_date=start_date,
        end_date=end_date,
    )
    return abs_path
