"""
KAVACH-07 — Database
SQLite schema, async queries, state persistence.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

import aiosqlite

from models import Signal, Position, TradeResult, RiskMetrics
from utils import get_logger


DB_PATH = "kavach07.db"

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────

SCHEMA_VERSION = 1

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS candles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    interval    TEXT    NOT NULL,
    open_time   INTEGER NOT NULL,
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      REAL    NOT NULL,
    quote_vol   REAL    DEFAULT 0,
    num_trades  INTEGER DEFAULT 0,
    UNIQUE(symbol, interval, open_time)
);
CREATE INDEX IF NOT EXISTS idx_candles_sym_int ON candles(symbol, interval, open_time DESC);

CREATE TABLE IF NOT EXISTS funding_rates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    funding_rate REAL    NOT NULL,
    funding_time INTEGER NOT NULL,
    mark_price   REAL    NOT NULL,
    index_price  REAL    NOT NULL,
    recorded_at  INTEGER NOT NULL,
    UNIQUE(symbol, funding_time)
);
CREATE INDEX IF NOT EXISTS idx_funding_sym ON funding_rates(symbol, funding_time DESC);

CREATE TABLE IF NOT EXISTS open_interest (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT NOT NULL,
    oi         REAL NOT NULL,
    oi_value   REAL NOT NULL,
    recorded_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oi_sym ON open_interest(symbol, recorded_at DESC);

CREATE TABLE IF NOT EXISTS signals (
    id           TEXT    PRIMARY KEY,
    symbol       TEXT    NOT NULL,
    strategy     TEXT    NOT NULL,
    direction    TEXT    NOT NULL,
    confidence   REAL    NOT NULL,
    ml_score     REAL    NOT NULL,
    entry_type   TEXT    NOT NULL,
    entry_price  REAL    NOT NULL,
    sl_price     REAL    NOT NULL,
    tp1_price    REAL    NOT NULL,
    tp2_price    REAL,
    risk_pct     REAL    NOT NULL,
    r_ratio      REAL    NOT NULL,
    atr          REAL    NOT NULL,
    rationale    TEXT    NOT NULL,
    timestamp    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(timestamp DESC);

CREATE TABLE IF NOT EXISTS positions (
    id            TEXT    PRIMARY KEY,
    symbol        TEXT    NOT NULL,
    direction     TEXT    NOT NULL,
    entry_price   REAL    NOT NULL,
    size          REAL    NOT NULL,
    sl_price      REAL    NOT NULL,
    tp1_price     REAL    NOT NULL,
    tp2_price     REAL,
    open_time     INTEGER NOT NULL,
    strategy      TEXT    NOT NULL,
    confidence    REAL    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'OPEN',
    pnl           REAL    DEFAULT 0,
    close_time    INTEGER,
    close_price   REAL,
    tp1_hit       INTEGER DEFAULT 0,
    expiry        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS trade_results (
    id                TEXT    PRIMARY KEY,
    position_id       TEXT    NOT NULL,
    symbol            TEXT    NOT NULL,
    strategy          TEXT    NOT NULL,
    direction         TEXT    NOT NULL,
    entry_price       REAL    NOT NULL,
    exit_price        REAL    NOT NULL,
    size              REAL    NOT NULL,
    pnl               REAL    NOT NULL,
    exit_reason       TEXT    NOT NULL,
    duration_seconds  REAL    NOT NULL,
    r_multiple        REAL    NOT NULL,
    confidence        REAL    NOT NULL,
    timestamp         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trade_results(timestamp DESC);

CREATE TABLE IF NOT EXISTS risk_state (
    id                   INTEGER PRIMARY KEY DEFAULT 1,
    balance              REAL    NOT NULL DEFAULT 1000.0,
    peak_balance         REAL    NOT NULL DEFAULT 1000.0,
    total_pnl            REAL    NOT NULL DEFAULT 0,
    gross_profit         REAL    NOT NULL DEFAULT 0,
    gross_loss           REAL    NOT NULL DEFAULT 0,
    total_trades         INTEGER NOT NULL DEFAULT 0,
    winning_trades       INTEGER NOT NULL DEFAULT 0,
    losing_trades        INTEGER NOT NULL DEFAULT 0,
    consecutive_losses   INTEGER NOT NULL DEFAULT 0,
    consecutive_wins     INTEGER NOT NULL DEFAULT 0,
    total_signals        INTEGER NOT NULL DEFAULT 0,
    daily_pnl            REAL    NOT NULL DEFAULT 0,
    daily_start_balance  REAL    NOT NULL DEFAULT 1000.0,
    circuit_state        TEXT    NOT NULL DEFAULT 'OK',
    circuit_reason       TEXT    NOT NULL DEFAULT '',
    halt_until           REAL,
    updated_at           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS error_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    component   TEXT    NOT NULL,
    message     TEXT    NOT NULL,
    traceback   TEXT,
    recorded_at INTEGER NOT NULL
);
"""


# ─────────────────────────────────────────────────────────────
# Database class
# ─────────────────────────────────────────────────────────────

def _ts() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class Database:
    def __init__(self, path: str = DB_PATH):
        self._path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_DDL)
        await self._migrate()
        await self._db.commit()
        logger.info(f"Database connected: {self._path}")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _migrate(self) -> None:
        async with self._db.execute("SELECT version FROM schema_version LIMIT 1") as cur:  # type: ignore
            row = await cur.fetchone()
        if row is None:
            await self._db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            await self._db.execute(
                "INSERT OR IGNORE INTO risk_state (id) VALUES (1)"
            )

    # ─── Candles ─────────────────────────────────────────────

    async def upsert_candle(self, c: dict) -> None:
        await self._db.execute(  # type: ignore
            """INSERT OR REPLACE INTO candles
               (symbol, interval, open_time, open, high, low, close, volume, quote_vol, num_trades)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (c["symbol"], c["interval"], int(c["open_time"]), float(c["open"]),
             float(c["high"]), float(c["low"]), float(c["close"]), float(c["volume"]),
             float(c.get("quote_volume", 0)), int(c.get("num_trades", 0))),
        )
        await self._db.commit()  # type: ignore

    async def get_candles(self, symbol: str, interval: str, limit: int = 300) -> List[dict]:
        async with self._db.execute(  # type: ignore
            "SELECT * FROM candles WHERE symbol=? AND interval=? ORDER BY open_time DESC LIMIT ?",
            (symbol, interval, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in reversed(rows)]

    # ─── Funding ─────────────────────────────────────────────

    async def upsert_funding(self, symbol: str, rate: float, fund_time: int,
                              mark: float, index: float) -> None:
        await self._db.execute(  # type: ignore
            """INSERT OR IGNORE INTO funding_rates
               (symbol, funding_rate, funding_time, mark_price, index_price, recorded_at)
               VALUES (?,?,?,?,?,?)""",
            (symbol, rate, fund_time, mark, index, _ts()),
        )
        await self._db.commit()  # type: ignore

    async def get_funding_history(self, symbol: str, limit: int = 360) -> List[float]:
        """Returns list of funding rates ordered by time ascending."""
        async with self._db.execute(  # type: ignore
            "SELECT funding_rate FROM funding_rates WHERE symbol=? ORDER BY funding_time DESC LIMIT ?",
            (symbol, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [float(r["funding_rate"]) for r in reversed(rows)]

    # ─── OI ──────────────────────────────────────────────────

    async def insert_oi(self, symbol: str, oi: float, oi_value: float) -> None:
        await self._db.execute(  # type: ignore
            "INSERT INTO open_interest (symbol, oi, oi_value, recorded_at) VALUES (?,?,?,?)",
            (symbol, oi, oi_value, _ts()),
        )
        await self._db.commit()  # type: ignore

    async def get_oi_history(self, symbol: str, limit: int = 300) -> List[tuple]:
        """Returns list of (oi, recorded_at) ordered ascending."""
        async with self._db.execute(  # type: ignore
            "SELECT oi, recorded_at FROM open_interest WHERE symbol=? ORDER BY recorded_at DESC LIMIT ?",
            (symbol, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [(float(r["oi"]), int(r["recorded_at"])) for r in reversed(rows)]

    # ─── Signals ─────────────────────────────────────────────

    async def insert_signal(self, sig: Signal) -> None:
        await self._db.execute(  # type: ignore
            """INSERT OR IGNORE INTO signals
               (id, symbol, strategy, direction, confidence, ml_score, entry_type,
                entry_price, sl_price, tp1_price, tp2_price, risk_pct, r_ratio, atr,
                rationale, timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sig.id, sig.symbol, sig.strategy, sig.direction, sig.confidence,
             sig.ml_score, sig.entry_type, sig.entry_price, sig.sl_price,
             sig.tp1_price, sig.tp2_price, sig.risk_pct, sig.r_ratio, sig.atr,
             sig.rationale, _ts()),
        )
        await self._db.commit()  # type: ignore

    async def get_recent_signals(self, limit: int = 20) -> List[dict]:
        async with self._db.execute(  # type: ignore
            "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def signal_exists_recently(self, symbol: str, strategy: str, window_ms: int = 180_000) -> bool:
        """True if same symbol+strategy signal fired in last window_ms milliseconds."""
        cutoff = _ts() - window_ms
        async with self._db.execute(  # type: ignore
            "SELECT 1 FROM signals WHERE symbol=? AND strategy=? AND timestamp>? LIMIT 1",
            (symbol, strategy, cutoff),
        ) as cur:
            return await cur.fetchone() is not None

    # ─── Positions ───────────────────────────────────────────

    async def insert_position(self, pos: Position) -> None:
        expiry_ts = int(pos.expiry.timestamp() * 1000) if pos.expiry else None
        await self._db.execute(  # type: ignore
            """INSERT OR IGNORE INTO positions
               (id, symbol, direction, entry_price, size, sl_price, tp1_price, tp2_price,
                open_time, strategy, confidence, status, pnl, expiry)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pos.id, pos.symbol, pos.direction, pos.entry_price, pos.size,
             pos.sl_price, pos.tp1_price, pos.tp2_price,
             int(pos.open_time.timestamp() * 1000),
             pos.strategy, pos.confidence, pos.status, pos.pnl, expiry_ts),
        )
        await self._db.commit()  # type: ignore

    async def update_position(self, pos: Position) -> None:
        close_ts = int(pos.close_time.timestamp() * 1000) if pos.close_time else None
        await self._db.execute(  # type: ignore
            """UPDATE positions SET status=?, pnl=?, close_time=?, close_price=?, tp1_hit=?
               WHERE id=?""",
            (pos.status, pos.pnl, close_ts, pos.close_price, int(pos.tp1_hit), pos.id),
        )
        await self._db.commit()  # type: ignore

    async def get_open_positions(self) -> List[dict]:
        async with self._db.execute(  # type: ignore
            "SELECT * FROM positions WHERE status='OPEN'"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_recent_positions(self, limit: int = 10) -> List[dict]:
        async with self._db.execute(  # type: ignore
            "SELECT * FROM positions ORDER BY open_time DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ─── Trade Results ────────────────────────────────────────

    async def insert_trade_result(self, tr: TradeResult) -> None:
        await self._db.execute(  # type: ignore
            """INSERT OR IGNORE INTO trade_results
               (id, position_id, symbol, strategy, direction, entry_price, exit_price,
                size, pnl, exit_reason, duration_seconds, r_multiple, confidence, timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(tr.timestamp.timestamp()), tr.position_id, tr.symbol, tr.strategy,
             tr.direction, tr.entry_price, tr.exit_price, tr.size, tr.pnl,
             tr.exit_reason, tr.duration_seconds, tr.r_multiple, tr.confidence, _ts()),
        )
        await self._db.commit()  # type: ignore

    async def get_recent_trades(self, limit: int = 20) -> List[dict]:
        async with self._db.execute(  # type: ignore
            "SELECT * FROM trade_results ORDER BY timestamp DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_strategy_stats(self) -> List[dict]:
        async with self._db.execute(  # type: ignore
            """SELECT strategy,
                      COUNT(*) as trades,
                      SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins,
                      SUM(pnl) as total_pnl,
                      AVG(r_multiple) as avg_r
               FROM trade_results
               GROUP BY strategy
               ORDER BY total_pnl DESC"""
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ─── Risk State ───────────────────────────────────────────

    async def save_risk_metrics(self, m: RiskMetrics) -> None:
        await self._db.execute(  # type: ignore
            """INSERT OR REPLACE INTO risk_state
               (id, balance, peak_balance, total_pnl, gross_profit, gross_loss,
                total_trades, winning_trades, losing_trades, consecutive_losses,
                consecutive_wins, total_signals, daily_pnl, daily_start_balance,
                circuit_state, circuit_reason, halt_until, updated_at)
               VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (m.balance, m.peak_balance, m.total_pnl, m.gross_profit, m.gross_loss,
             m.total_trades, m.winning_trades, m.losing_trades, m.consecutive_losses,
             m.consecutive_wins, m.total_signals, m.daily_pnl, m.daily_start_balance,
             m.circuit_state, m.circuit_reason, m.halt_until, _ts()),
        )
        await self._db.commit()  # type: ignore

    async def load_risk_metrics(self) -> Optional[dict]:
        async with self._db.execute(  # type: ignore
            "SELECT * FROM risk_state WHERE id=1"
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    # ─── Error Log ────────────────────────────────────────────

    async def log_error(self, component: str, message: str, tb: str = "") -> None:
        try:
            await self._db.execute(  # type: ignore
                "INSERT INTO error_log (component, message, traceback, recorded_at) VALUES (?,?,?,?)",
                (component, message[:500], tb[:2000], _ts()),
            )
            await self._db.commit()  # type: ignore
        except Exception:
            pass  # Never crash on error logging

    # ─── Cleanup (prevent unbounded growth) ──────────────────

    async def cleanup_old_data(self, days: int = 30) -> None:
        cutoff = _ts() - days * 86_400_000
        for table in ("candles", "signals", "error_log"):
            await self._db.execute(  # type: ignore
                f"DELETE FROM {table} WHERE recorded_at < ? OR timestamp < ?",
                (cutoff, cutoff),
            )
        # OI: keep last 5000 rows per symbol
        await self._db.execute(  # type: ignore
            """DELETE FROM open_interest WHERE id NOT IN (
               SELECT id FROM open_interest ORDER BY recorded_at DESC LIMIT 50000)"""
        )
        await self._db.commit()  # type: ignore
        logger.debug("Old data cleaned up")
