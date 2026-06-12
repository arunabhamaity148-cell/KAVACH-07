"""
KAVACH-07 — Database Layer
Async SQLite with WAL mode, migrations, and all CRUD operations.
"""
from __future__ import annotations

import aiosqlite
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import Config
from models import Candle, Position, RiskMetrics, Signal, TradeResult
from utils import get_logger

logger = get_logger(__name__)

class Database:

    def __init__(self, config: Config):
        self._cfg = config
        self._db: Optional[aiosqlite.Connection] = None
        self._last_commit_time = time.time()

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._cfg.DB_PATH)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA cache_size=-64000")
        # FIX: Auto-checkpoint to prevent WAL file growth
        await self._db.execute("PRAGMA wal_autocheckpoint=1000")
        await self._migrate()
        logger.info(f"Database connected: {self._cfg.DB_PATH}")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            logger.info("Database closed")

    async def _migrate(self) -> None:
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, interval TEXT NOT NULL,
                open_time INTEGER NOT NULL, open REAL, high REAL, low REAL,
                close REAL, volume REAL, close_time INTEGER,
                quote_volume REAL, num_trades INTEGER, taker_buy_base REAL,
                UNIQUE(symbol, interval, open_time)
            );
            CREATE INDEX IF NOT EXISTS idx_candles_sym_tf ON candles(symbol, interval, open_time DESC);

            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY, symbol TEXT NOT NULL, strategy TEXT NOT NULL,
                direction TEXT NOT NULL, confidence REAL, entry_type TEXT,
                entry_price REAL, sl_price REAL, tp1_price REAL, tp2_price REAL,
                risk_pct REAL, r_ratio REAL, atr REAL, ml_score REAL,
                rationale TEXT, timestamp INTEGER, filters TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(timestamp DESC);

            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY, symbol TEXT NOT NULL, direction TEXT NOT NULL,
                entry_price REAL, size REAL, sl_price REAL, tp1_price REAL,
                tp2_price REAL, open_time INTEGER, strategy TEXT, confidence REAL,
                status TEXT, pnl REAL DEFAULT 0, close_time INTEGER,
                close_price REAL, tp1_hit INTEGER DEFAULT 0, expiry INTEGER,
                reserved_risk REAL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT,
                symbol TEXT, strategy TEXT, direction TEXT, entry_price REAL,
                exit_price REAL, size REAL, pnl REAL, exit_reason TEXT,
                duration_seconds REAL, r_multiple REAL, confidence REAL,
                close_time INTEGER, reserved_risk REAL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(close_time DESC);

            CREATE TABLE IF NOT EXISTS risk_state (
                id INTEGER PRIMARY KEY CHECK(id = 1), balance REAL,
                peak_balance REAL, total_pnl REAL, gross_profit REAL,
                gross_loss REAL, total_trades INTEGER, winning_trades INTEGER,
                losing_trades INTEGER, consecutive_losses INTEGER,
                consecutive_wins INTEGER, total_signals INTEGER, daily_pnl REAL,
                daily_start_balance REAL, circuit_state TEXT, circuit_reason TEXT,
                halt_until REAL, paused INTEGER, updated_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS health_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER,
                ws_alive INTEGER, data_fresh INTEGER, signals_flowing INTEGER,
                no_errors INTEGER, error_count INTEGER, ws_reconnects INTEGER,
                uptime_seconds REAL, memory_mb REAL
            );
            """
        )
        await self._db.commit()

        # FIX: Specific exception handling for migration
        try:
            await self._db.execute("ALTER TABLE risk_state ADD COLUMN paused INTEGER NOT NULL DEFAULT 0")
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass  # Column already exists — expected
        except Exception as e:
            logger.error(f"Migration error: {e}")
            raise

    async def insert_candle(self, candle: Candle) -> None:
        await self._db.execute(
            """INSERT INTO candles(symbol, interval, open_time, open, high, low, close,
                volume, close_time, quote_volume, num_trades, taker_buy_base)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, interval, open_time) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume,
                close_time=excluded.close_time, quote_volume=excluded.quote_volume,
                num_trades=excluded.num_trades, taker_buy_base=excluded.taker_buy_base""",
            (candle.symbol, candle.interval, int(candle.open_time.timestamp() * 1000),
             candle.open, candle.high, candle.low, candle.close, candle.volume,
             int(candle.close_time.timestamp() * 1000), candle.quote_volume,
             candle.num_trades, candle.taker_buy_base),
        )
        await self._maybe_commit()

    async def get_candles(self, symbol: str, interval: str, limit: int = 200) -> List[Dict]:
        async with self._db.execute(
            "SELECT * FROM candles WHERE symbol=? AND interval=? ORDER BY open_time DESC LIMIT ?",
            (symbol, interval, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in reversed(rows)]

    async def insert_signal(self, signal: Signal) -> None:
        await self._db.execute(
            """INSERT INTO signals(id, symbol, strategy, direction, confidence,
                entry_type, entry_price, sl_price, tp1_price, tp2_price,
                risk_pct, r_ratio, atr, ml_score, rationale, timestamp, filters)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (signal.id, signal.symbol, signal.strategy, signal.direction,
             signal.confidence, signal.entry_type, signal.entry_price,
             signal.sl_price, signal.tp1_price, signal.tp2_price,
             signal.risk_pct, signal.r_ratio, signal.atr,
             signal.ml_score, signal.rationale,
             int(signal.timestamp.timestamp() * 1000),
             json.dumps(signal.filters_passed)),
        )
        await self._maybe_commit()

    async def get_recent_signals(self, limit: int = 20) -> List[Dict]:
        async with self._db.execute(
            "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def signal_exists(self, symbol: str, strategy: str, since_ms: int) -> bool:
        async with self._db.execute(
            "SELECT 1 FROM signals WHERE symbol=? AND strategy=? AND timestamp > ? LIMIT 1",
            (symbol, strategy, since_ms),
        ) as cur:
            return await cur.fetchone() is not None

    async def insert_position(self, pos: Position) -> None:
        await self._db.execute(
            """INSERT INTO positions(id, symbol, direction, entry_price, size,
                sl_price, tp1_price, tp2_price, open_time, strategy, confidence,
                status, pnl, expiry, reserved_risk)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pos.id, pos.symbol, pos.direction, pos.entry_price, pos.size,
             pos.sl_price, pos.tp1_price, pos.tp2_price,
             int(pos.open_time.timestamp() * 1000),
             pos.strategy, pos.confidence, pos.status, pos.pnl,
             int(pos.expiry.timestamp() * 1000) if pos.expiry else None,
             pos.reserved_risk),
        )
        await self._maybe_commit()

    async def update_position(self, pos: Position) -> None:
        # FIX: Save updated sl_price and size after partial close
        await self._db.execute(
            """UPDATE positions SET
                status = ?, pnl = ?, close_time = ?, close_price = ?,
                tp1_hit = ?, sl_price = ?, size = ?, reserved_risk = ?
            WHERE id = ?""",
            (pos.status, pos.pnl,
             int(pos.close_time.timestamp() * 1000) if pos.close_time else None,
             pos.close_price, int(pos.tp1_hit), pos.sl_price, pos.size,
             pos.reserved_risk, pos.id),
        )
        await self._maybe_commit()

    async def get_open_positions(self) -> List[Dict]:
        # FIX: Include TP1_HIT positions
        async with self._db.execute(
            "SELECT * FROM positions WHERE status IN ('OPEN', 'TP1_HIT')"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def insert_trade_result(self, result: TradeResult) -> None:
        await self._db.execute(
            """INSERT INTO trades(position_id, symbol, strategy, direction,
                entry_price, exit_price, size, pnl, exit_reason,
                duration_seconds, r_multiple, confidence, close_time, reserved_risk)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (result.position_id, result.symbol, result.strategy, result.direction,
             result.entry_price, result.exit_price, result.size, result.pnl,
             result.exit_reason, result.duration_seconds, result.r_multiple,
             result.confidence, int(result.timestamp.timestamp() * 1000),
             result.reserved_risk),
        )
        await self._maybe_commit()

    async def get_recent_trades(self, limit: int = 20) -> List[Dict]:
        async with self._db.execute(
            "SELECT * FROM trades ORDER BY close_time DESC LIMIT ?", (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_trade_stats(self) -> Dict[str, Any]:
        async with self._db.execute(
            """SELECT COUNT(*) as total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                SUM(pnl) as total_pnl, AVG(pnl) as avg_pnl,
                AVG(CASE WHEN pnl > 0 THEN pnl END) as avg_win,
                AVG(CASE WHEN pnl < 0 THEN pnl END) as avg_loss,
                AVG(r_multiple) as avg_r FROM trades"""
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}

    async def save_risk_metrics(self, metrics: RiskMetrics) -> None:
        await self._db.execute(
            """INSERT INTO risk_state(id, balance, peak_balance, total_pnl,
                gross_profit, gross_loss, total_trades, winning_trades,
                losing_trades, consecutive_losses, consecutive_wins,
                total_signals, daily_pnl, daily_start_balance, circuit_state,
                circuit_reason, halt_until, paused, updated_at)
            VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                balance=excluded.balance, peak_balance=excluded.peak_balance,
                total_pnl=excluded.total_pnl, gross_profit=excluded.gross_profit,
                gross_loss=excluded.gross_loss, total_trades=excluded.total_trades,
                winning_trades=excluded.winning_trades,
                losing_trades=excluded.losing_trades,
                consecutive_losses=excluded.consecutive_losses,
                consecutive_wins=excluded.consecutive_wins,
                total_signals=excluded.total_signals,
                daily_pnl=excluded.daily_pnl,
                daily_start_balance=excluded.daily_start_balance,
                circuit_state=excluded.circuit_state,
                circuit_reason=excluded.circuit_reason,
                halt_until=excluded.halt_until, paused=excluded.paused,
                updated_at=excluded.updated_at""",
            (metrics.balance, metrics.peak_balance, metrics.total_pnl,
             metrics.gross_profit, metrics.gross_loss, metrics.total_trades,
             metrics.winning_trades, metrics.losing_trades,
             metrics.consecutive_losses, metrics.consecutive_wins,
             metrics.total_signals, metrics.daily_pnl, metrics.daily_start_balance,
             metrics.circuit_state, metrics.circuit_reason,
             metrics.halt_until, int(metrics.paused), int(time.time())),
            ),
        )
        await self._maybe_commit()

    async def load_risk_metrics(self) -> Optional[Dict]:
        async with self._db.execute("SELECT * FROM risk_state WHERE id = 1") as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def insert_health_log(self, health: Dict[str, Any]) -> None:
        await self._db.execute(
            """INSERT INTO health_log(timestamp, ws_alive, data_fresh,
                signals_flowing, no_errors, error_count, ws_reconnects,
                uptime_seconds, memory_mb)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (int(time.time()), int(health.get("ws_alive", 0)),
             int(health.get("data_fresh", 0)),
             int(health.get("signals_flowing", 0)),
             int(health.get("no_errors", 0)),
             health.get("error_count", 0),
             health.get("ws_reconnects", 0),
             health.get("uptime_seconds", 0.0),
             health.get("memory_mb", 0.0)),
        )
        await self._maybe_commit()

    # FIX: Batch commits every 1 second instead of every insert
    async def _maybe_commit(self) -> None:
        now = time.time()
        if now - self._last_commit_time >= 1.0:
            await self._db.commit()
            self._last_commit_time = now

    async def commit(self) -> None:
        if self._db:
            await self._db.commit()
            self._last_commit_time = time.time()
