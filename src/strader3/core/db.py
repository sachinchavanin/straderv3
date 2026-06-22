"""SQLite database schema and connection for trade persistence."""

import aiosqlite

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    client_order_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT,
    order_type TEXT,
    product_type TEXT,
    quantity INTEGER,
    filled_quantity INTEGER,
    price REAL,
    trigger_price REAL,
    status TEXT,
    average_price REAL,
    created_at TIMESTAMP,
    submitted_at TIMESTAMP,
    filled_at TIMESTAMP,
    cancelled_at TIMESTAMP,
    signal_id TEXT,
    rejection_reason TEXT,
    broker_message TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    state TEXT,
    quantity INTEGER,
    average_price REAL,
    current_price REAL,
    entry_order_id TEXT,
    entry_time TIMESTAMP,
    entry_signal_id TEXT,
    strategy_name TEXT,
    stop_loss REAL,
    target REAL,
    trailing_stop REAL,
    max_holding_until TIMESTAMP,
    realized_pnl REAL,
    unrealized_pnl REAL,
    sector TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    strategy_name TEXT,
    entry_time TIMESTAMP,
    entry_price REAL,
    entry_quantity INTEGER,
    entry_order_id TEXT,
    exit_time TIMESTAMP,
    exit_price REAL,
    exit_quantity INTEGER,
    exit_order_id TEXT,
    exit_reason TEXT,
    gross_pnl REAL,
    charges REAL DEFAULT 0.0,
    net_pnl REAL,
    signal_price REAL,
    entry_slippage REAL,
    exit_slippage REAL,
    execution_delay_ms INTEGER,
    signal_id TEXT,
    sector TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    date TEXT PRIMARY KEY,
    realized_pnl REAL DEFAULT 0.0,
    unrealized_pnl REAL DEFAULT 0.0,
    total_pnl REAL DEFAULT 0.0,
    trades_count INTEGER DEFAULT 0,
    wins_count INTEGER DEFAULT 0,
    losses_count INTEGER DEFAULT 0,
    charges REAL DEFAULT 0.0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_signal_id ON orders(signal_id);
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
CREATE INDEX IF NOT EXISTS idx_positions_state ON positions(state);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time);
"""


async def init_db(db_path: str) -> None:
    """Initialize SQLite database with schema."""
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(DB_SCHEMA)
        await db.commit()
