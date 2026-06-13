"""
KAVACH-07 — DB Manager
Async SQLite interface via aiosqlite. Thread-safe, WAL-mode, comprehensive CRUD.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class DBManager:
    """Asynchronous SQLite manager for KAVACH-07.

    Usage:
        db = DBManager("kavach.db")
        await db.initialize()
        ...
        await db.close()
    """

    def __init__(self, db_path: str = "kavach.db") -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    # ──────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Open DB connection and apply schema."""
        try:
            os.makedirs(os.path.dirname(self._db_path) if os.path.dirname(self._db_path) else ".", exist_ok=True)
            self._conn = await aiosqlite.connect(self._db_path)
            self._conn.row_factory = aiosqlite.Row
            # Apply schema
            schema_sql = _SCHEMA_PATH.read_text()
            await self._conn.executescript(schema_sql)
            await self._conn.commit()
            logger.info("DBManager initialised at %s", self._db_path)
        except Exception as exc:
            logger.critical("DBManager.initialize failed: %s", exc, exc_info=True)
            raise

    async def close(self) -> None:
        """Close the database connection cleanly."""
        try:
            if self._conn:
                await self._conn.close()
                logger.info("DBManager closed.")
        except Exception as exc:
            logger.error("DBManager.close error: %s", exc)

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    async def _execute(self, sql: str, params: tuple = ()) -> None:
        """Execute a write statement with locking."""
        async with self._lock:
            try:
                await self._conn.execute(sql, params)
                await self._conn.commit()
            except Exception as exc:
                logger.error("DB execute error: %s | SQL: %.120s", exc, sql)
                raise

    async def _fetchall(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """Execute a SELECT and return list of dicts."""
        try:
            async with self._conn.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as exc:
            logger.error("DB fetchall error: %s | SQL: %.120s", exc, sql)
            raise  # Do not swallow errors for zero-silent-failure goal

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        """Execute a SELECT and return one dict or None."""
        try:
            async with self._conn.execute(sql, params) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
        except Exception as exc:
            logger.error("DB fetchone error: %s | SQL: %.120s", exc, sql)
            raise  # Do not swallow errors for zero-silent-failure goal

    # ──────────────────────────────────────────────────────────────────────────
    # market_data
    # ──────────────────────────────────────────────────────────────────────────

    async def insert_market_data(self, md: Any) -> None:
        """Persist a MarketData snapshot. md is a MarketData dataclass instance."""
        sql = """
            INSERT OR REPLACE INTO market_data (
                timestamp, symbol, price, volume, open_interest, funding_rate,
                adx, atr, vwap, cvd, spot_volume, hyperliquid_price,
                hyperliquid_funding, fng_index, etf_net_flow, stablecoin_net_flow,
                long_liq_cluster_price, long_liq_cluster_size,
                short_liq_cluster_price, short_liq_cluster_size
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        lc = md.liquidation_clusters
        params = (
            int(md.timestamp * 1000), md.symbol, md.price, md.volume,
            md.open_interest, md.funding_rate, md.adx, md.atr, md.vwap,
            md.cvd, md.spot_volume, md.hyperliquid_price, md.hyperliquid_funding,
            md.fng_index, md.etf_net_flow, md.stablecoin_net_flow,
            lc.get("long_cluster_price", 0.0), lc.get("long_cluster_size", 0.0),
            lc.get("short_cluster_price", 0.0), lc.get("short_cluster_size", 0.0),
        )
        await self._execute(sql, params)

    async def get_latest_market_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM market_data WHERE symbol=? ORDER BY timestamp DESC LIMIT 1"
        return await self._fetchone(sql, (symbol,))

    async def get_market_data_history(
        self, symbol: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM market_data WHERE symbol=? ORDER BY timestamp DESC LIMIT ?"
        return await self._fetchall(sql, (symbol, limit))

    # ──────────────────────────────────────────────────────────────────────────
    # signals
    # ──────────────────────────────────────────────────────────────────────────

    async def insert_signal(self, sig: Any) -> int:
        """Insert a MetaSignal and return its rowid."""
        sql = """
            INSERT INTO signals (
                timestamp, symbol, side, confidence, entry, stop_loss,
                take_profit, position_size_usdt, strategies_fired, rationale, regime
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """
        strategies_json = json.dumps(
            sig.strategies_fired if hasattr(sig, "strategies_fired") else []
        )
        params = (
            int(sig.timestamp * 1000), sig.symbol, sig.side, sig.confidence,
            sig.entry, sig.stop_loss, sig.take_profit,
            getattr(sig, "position_size_usdt", 0.0),
            strategies_json, sig.rationale,
            getattr(sig, "regime", "UNDEFINED"),
        )
        async with self._lock:
            try:
                cursor = await self._conn.execute(sql, params)
                await self._conn.commit()
                return cursor.lastrowid
            except Exception as exc:
                logger.error("insert_signal error: %s", exc)
                raise

    async def confirm_signal(self, signal_id: int) -> None:
        sql = "UPDATE signals SET confirmed=1 WHERE id=?"
        await self._execute(sql, (signal_id,))

    async def get_signal_by_id(self, signal_id: int) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM signals WHERE id=?"
        row = await self._fetchone(sql, (signal_id,))
        if row:
            row["strategies_fired"] = json.loads(row.get("strategies_fired") or "[]")
        return row

    async def get_latest_signals(self, limit: int = 10) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?"
        rows = await self._fetchall(sql, (limit,))
        for r in rows:
            r["strategies_fired"] = json.loads(r.get("strategies_fired") or "[]")
        return rows

    # ──────────────────────────────────────────────────────────────────────────
    # trades
    # ──────────────────────────────────────────────────────────────────────────

    async def insert_trade(
        self, signal_id: int, entry_price: float, open_time: Optional[float] = None
    ) -> int:
        sql = """
            INSERT INTO trades (signal_id, entry_price, status, open_time)
            VALUES (?,?,'OPEN',?)
        """
        ts = int((open_time or time.time()) * 1000)
        async with self._lock:
            try:
                cursor = await self._conn.execute(sql, (signal_id, entry_price, ts))
                await self._conn.commit()
                return cursor.lastrowid
            except Exception as exc:
                logger.error("insert_trade error: %s", exc)
                raise

    async def update_trade_status(
        self,
        trade_id: int,
        status: str,
        exit_price: Optional[float] = None,
        pnl: Optional[float] = None,
        pnl_percent: Optional[float] = None,
    ) -> None:
        sql = """
            UPDATE trades
            SET status=?, exit_price=?, pnl=?, pnl_percent=?, close_time=?
            WHERE id=?
        """
        params = (
            status, exit_price, pnl, pnl_percent,
            int(time.time() * 1000), trade_id,
        )
        await self._execute(sql, params)

    async def get_open_trades(self) -> List[Dict[str, Any]]:
        sql = """
            SELECT t.*, s.symbol, s.side, s.entry, s.stop_loss, s.take_profit
            FROM trades t
            JOIN signals s ON t.signal_id = s.id
            WHERE t.status='OPEN'
            ORDER BY t.open_time DESC
        """
        return await self._fetchall(sql)

    async def get_all_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        sql = """
            SELECT t.*, s.symbol, s.side
            FROM trades t
            JOIN signals s ON t.signal_id = s.id
            ORDER BY t.open_time DESC LIMIT ?
        """
        return await self._fetchall(sql, (limit,))

    async def get_daily_pnl(self, date_ts_start: int, date_ts_end: int) -> float:
        """Return total realised PnL between two epoch-ms timestamps."""
        sql = """
            SELECT COALESCE(SUM(pnl), 0.0) as total
            FROM trades
            WHERE close_time >= ? AND close_time < ? AND status != 'OPEN'
        """
        row = await self._fetchone(sql, (date_ts_start, date_ts_end))
        return float(row["total"]) if row else 0.0

    # ──────────────────────────────────────────────────────────────────────────
    # strategy_perf
    # ──────────────────────────────────────────────────────────────────────────

    async def update_strategy_performance(
        self,
        strategy_name: str,
        symbol: str,
        is_win: bool,
        confidence: float,
    ) -> None:
        """Upsert win/loss stats for a strategy."""
        sql_select = "SELECT * FROM strategy_perf WHERE strategy_name=? AND symbol=?"
        row = await self._fetchone(sql_select, (strategy_name, symbol))
        if row is None:
            sql = """
                INSERT INTO strategy_perf
                    (strategy_name, symbol, total_signals, wins, losses,
                     win_rate, profit_factor, avg_confidence, last_updated)
                VALUES (?,?,1,?,?,?,1.0,?,?)
            """
            wins = 1 if is_win else 0
            losses = 0 if is_win else 1
            win_rate = 100.0 if is_win else 0.0
            await self._execute(
                sql,
                (strategy_name, symbol, wins, losses, win_rate,
                 confidence, int(time.time() * 1000)),
            )
        else:
            total = row["total_signals"] + 1
            wins = row["wins"] + (1 if is_win else 0)
            losses = row["losses"] + (0 if is_win else 1)
            win_rate = 100.0 * wins / total
            avg_conf = (row["avg_confidence"] * row["total_signals"] + confidence) / total
            profit_factor = wins / max(losses, 1)
            sql = """
                UPDATE strategy_perf
                SET total_signals=?, wins=?, losses=?, win_rate=?,
                    profit_factor=?, avg_confidence=?, last_updated=?
                WHERE strategy_name=? AND symbol=?
            """
            await self._execute(
                sql,
                (total, wins, losses, win_rate, profit_factor, avg_conf,
                 int(time.time() * 1000), strategy_name, symbol),
            )

    async def get_strategy_performance(
        self, strategy_name: str, symbol: str
    ) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM strategy_perf WHERE strategy_name=? AND symbol=?"
        return await self._fetchone(sql, (strategy_name, symbol))

    async def get_all_strategy_performance(self) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM strategy_perf ORDER BY win_rate DESC"
        return await self._fetchall(sql)

    # ──────────────────────────────────────────────────────────────────────────
    # bot_events
    # ──────────────────────────────────────────────────────────────────────────

    async def log_event(self, level: str, component: str, message: str) -> None:
        sql = "INSERT INTO bot_events (timestamp, level, component, message) VALUES (?,?,?,?)"
        await self._execute(sql, (int(time.time() * 1000), level, component, message))
