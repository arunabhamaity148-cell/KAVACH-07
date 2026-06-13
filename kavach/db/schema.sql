-- KAVACH-07 Database Schema v7.0.0 --

-- 1. Signals Table: Stores generated MetaSignals --
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    confidence REAL NOT NULL,
    entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    rationale TEXT,
    strategies_fired TEXT, -- JSON array of strategy names
    regime TEXT,
    position_size_usdt REAL,
    confirmed INTEGER DEFAULT 0 -- 0: Pending, 1: Executed, -1: Rejected
);

-- 2. Trades Table: Tracks execution and PnL --
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    size_usdt REAL NOT NULL,
    status TEXT DEFAULT 'OPEN', -- 'OPEN', 'CLOSED', 'CANCELLED'
    open_timestamp REAL NOT NULL,
    exit_price REAL,
    pnl REAL,
    close_timestamp REAL,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

-- 3. Market Data Table: Historical snapshots for backtesting/analytics --
CREATE TABLE IF NOT EXISTS market_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    open_interest REAL,
    funding_rate REAL
);

-- 4. Logs Table: Internal bot audit trail --
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    level TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT NOT NULL
);

-- Indices for performance --
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_close_ts ON trades(close_timestamp);
CREATE INDEX IF NOT EXISTS idx_market_data_sym_ts O