"""
KAVACH-07 — DB Manager
Asynchronous SQLite interface for persistence and PnL tracking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite
import pytz

logger = logging.getLogger("kavach.db_manager")

class DBManager:
    """
    Handles all database interactions using aiosqlite.
    Enforces IST-based daily PnL resets and thread-safe operations.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._ist = pytz.timezone("Asia/Kolkata")

    async def initialize(self) -> None:
        """Opens connection and applies schema."""
        try:
            # Ensure directory exists
            db_dir = os.path.dirname(self._db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)

            self._conn = await aiosqlite.connect(self._db_path)
            self._conn.row_factory = aiosqlite.Row
            
            # Load and execute schema.sql (next file in delivery)
            schema_path = Path(__file__).parent / "schema.sql"
            if schema_path.exists():
                schema_sql = schema_path.read_text()
                await self._conn.executescript(schema_sql)
                await self._conn.commit()
            
            logger.info("Database initialized at %s", self._db_path)
        except Exception as e:
            logger.critical("Failed to initialize database: %s", e)
            raise

    async def close(self) -> None:
        """Graceful shutdown of DB connection."""
        if self._conn:
            await self._conn.close()
            logger.info("Database connection closed")

    # ──────────────────────────────────────────────────────────────────────────
    # Signals & Trades
    # ──────────────────────────────────────────────────────────────────────────

    async def insert_signal(self, signal: Any) -> int:
        """Persists a MetaSignal and returns its primary key."""
        async with self._lock:
            query = """
                INSERT INTO signals (
                    timestamp, symbol, side, confidence, entry, stop_loss, 
                    take_profit, rationale, strategies_fired, regime, position_size_usdt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                signal.timestamp,
                signal.symbol,
                signal.side,
                signal.confidence,
                signal.entry,
                signal.stop_loss,
                signal.take_profit,
                signal.rationale,
                json.dumps(signal.strategies_fired),
                signal.regime,
                signal.position_size_usdt
            )
            cursor = await self._conn.execute(query, params)
            await self._conn.commit()
            return cursor.lastrowid

    async def insert_trade(self, signal_id: int, symbol: str, side: str, entry: float, size: float) -> int:
        """Records an open trade."""
        async with self._lock:
            query = """
                INSERT INTO trades (
                    signal_id, symbol, side, entry_price, size_usdt, status, open_timestamp
                ) VALUES (?, ?, ?, ?, ?, 'OPEN', ?)
            """
            params = (signal_id, symbol, side, entry, size, time.time())
            cursor = await self._conn.execute(query, params)
            await self._conn.commit()
            return cursor.lastrowid

    async def close_trade(self, trade_id: int, exit_price: float, pnl: float) -> None:
        """Updates a trade to CLOSED status with realized PnL."""
        async with self._lock:
            query = """
                UPDATE trades 
                SET status = 'CLOSED', 
                    exit_price = ?, 
                    pnl = ?, 
                    close_timestamp = ? 
                WHERE id = ?
            """
            await self._conn.execute(query, (exit_price, pnl, time.time(), trade_id))
            await self._conn.commit()

    async def get_open_trades(self) -> List[Dict[str, Any]]:
        """Retrieves currently open positions."""
        query = "SELECT * FROM trades WHERE status = 'OPEN'"
        async with self._conn.execute(query) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ──────────────────────────────────────────────────────────────────────────
    # PnL & Analytics
    # ──────────────────────────────────────────────────────────────────────────

    async def get_daily_pnl(self) -> float:
        """Calculates realized PnL for the current IST day (since 00:00 IST)."""
        now_ist = datetime.now(self._ist)
        start_of_day_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ts = start_of_day_ist.timestamp()

        query = "SELECT SUM(pnl) as total FROM trades WHERE status = 'CLOSED' AND close_timestamp >= ?"
        async with self._conn.execute(query, (start_ts,)) as cursor:
            row = await cursor.fetchone()
            return float(row["total"]) if row and row["total"] is not None else 0.0

    async def get_total_pnl(self) -> float:
        """Calculates all-time realized PnL."""
        query = "SELECT SUM(pnl) as total FROM trades WHERE status = 'CLOSED'"
        async with self._conn.execute(query) as cursor:
            row = await cursor.fetchone()
            return float(row["total"]) if row and row["total"] is not None else 0.0

    # ──────────────────────────────────────────────────────────────────────────
    # Market Data Persistence
    # ──────────────────────────────────────────────────────────────────────────

    async def save_market_snapshot(self, symbol: str, price: float, oi: float, funding: float) -> None:
        """Periodically saves market snapshots for analysis."""
        async with self._lock:
            query = "INSERT INTO market_data (timestamp, symbol, price, open_interest, funding_rate) VALUES (?, ?, ?, ?, ?)"
            await self._conn.execute(query, (time.time(), symbol, price, oi, funding))
            await self._conn.commit()

    async def log_event(self, level: str, component: str, message: str) -> None:
        """Internal audit logger for DB."""
        async with self._lock:
            query = "INSERT INTO logs (timestamp, level, component, message) VALUES (?, ?, ?, ?)"
            await self._conn.execute(query, (time.time(), level, component, message))
            await self._conn.commit()