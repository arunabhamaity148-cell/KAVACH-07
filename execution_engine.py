"""
KAVACH-07 — Execution Engine (Paper Trade Only)
Simulates fills, tracks positions, checks TP/SL every second.
All state persists to SQLite via RiskManager.
"""
from __future__ import annotations

import asyncio
import random
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Callable, Awaitable

from config import Config
from data_engine import DataEngine
from database import Database
from models import Position, Signal, TradeResult
from risk_manager import RiskManager
from utils import get_logger

logger = get_logger(__name__)

# Paper trade simulation parameters
_SLIPPAGE_BPS_BASE  = 2       # 2bps base slippage
_SLIPPAGE_BPS_SPIKE = 8       # up to 8bps in volatile markets
_MISSED_FILL_RATE   = 0.08    # 8% chance LIMIT order misses
_POSITION_EXPIRY_HOURS = 24   # Auto-close after 24h


class ExecutionEngine:
    """
    Paper trade position manager.
    - Simulates order fills (with slippage + miss probability)
    - Heartbeat checks TP/SL every HEARTBEAT_INTERVAL seconds
    - Persists all positions to SQLite
    - Callbacks for trade closed events
    """

    def __init__(self, config: Config, data_engine: DataEngine,
                 db: Database, risk_manager: RiskManager):
        self._cfg = config
        self._de = data_engine
        self._db = db
        self._rm = risk_manager

        # In-memory position cache (keyed by position ID)
        self._positions: Dict[str, Position] = {}

        self._on_trade_closed_cbs: List[Callable[[TradeResult], Awaitable[None]]] = []
        self._on_position_opened_cbs: List[Callable[[Position], Awaitable[None]]] = []

        self._shutdown = False
        self._hb_task: Optional[asyncio.Task] = None

    # ─── Lifecycle ───────────────────────────────────────────

    async def start(self) -> None:
        """Load open positions from DB and start heartbeat."""
        await self._load_positions()
        self._hb_task = asyncio.create_task(self._heartbeat_loop(), name="execution_hb")
        logger.info(f"ExecutionEngine started ({len(self._positions)} open positions)")

    async def stop(self) -> None:
        self._shutdown = True
        if self._hb_task:
            self._hb_task.cancel()
            await asyncio.gather(self._hb_task, return_exceptions=True)
        logger.info("ExecutionEngine stopped")

    # ─── Callbacks ───────────────────────────────────────────

    def on_trade_closed(self, cb: Callable[[TradeResult], Awaitable[None]]) -> None:
        self._on_trade_closed_cbs.append(cb)

    def on_position_opened(self, cb: Callable[[Position], Awaitable[None]]) -> None:
        self._on_position_opened_cbs.append(cb)

    # ─── Signal → Paper Trade ────────────────────────────────

    async def execute_signal(self, signal: Signal) -> Optional[Position]:
        """
        Attempt to paper-trade a signal.
        Returns Position if filled, None if rejected (risk/limit/miss).
        """
        # Risk check
        size, rejection_reason = self._rm.calculate_size(signal)
        if size <= 0:
            logger.info(f"Signal rejected by risk manager: {rejection_reason} | {signal.symbol}")
            return None

        # Simulate fill
        fill_price = self._simulate_fill(signal)
        if fill_price is None:
            logger.info(f"LIMIT order missed: {signal.symbol} {signal.strategy}")
            return None

        # Create position
        now = datetime.now(timezone.utc)
        pos = Position(
            id=str(uuid.uuid4())[:12],
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=fill_price,
            size=size,
            sl_price=signal.sl_price,
            tp1_price=signal.tp1_price,
            tp2_price=signal.tp2_price,
            open_time=now,
            strategy=signal.strategy,
            confidence=signal.confidence,
            status="OPEN",
            expiry=now + timedelta(hours=_POSITION_EXPIRY_HOURS),
        )

        # Register with risk manager (reserve exposure)
        self._rm.on_position_opened(pos)

        # Persist
        self._positions[pos.id] = pos
        await self._db.insert_position(pos)

        logger.info(
            f"PAPER TRADE OPENED | {signal.symbol} {signal.direction} | "
            f"Entry: {fill_price:.4f} | SL: {pos.sl_price:.4f} | "
            f"TP1: {pos.tp1_price:.4f} | Size: {size:.4f} | "
            f"Strategy: {signal.strategy}"
        )

        # Fire callbacks
        for cb in self._on_position_opened_cbs:
            asyncio.create_task(cb(pos))

        return pos

    def _simulate_fill(self, signal: Signal) -> Optional[float]:
        """
        Simulate order fill with slippage.
        LIMIT orders may miss (MISSED_FILL_RATE).
        MARKET orders always fill.
        """
        current = self._de.get_current_price(signal.symbol)
        if current < 1e-10:
            current = signal.entry_price

        if signal.entry_type == "LIMIT":
            # Check if price is close enough to entry
            dist_pct = abs(current - signal.entry_price) / signal.entry_price
            if dist_pct > 0.003:  # Entry price more than 0.3% from current
                # Simulate miss
                if random.random() < _MISSED_FILL_RATE + dist_pct * 5:
                    return None

            fill_price = signal.entry_price  # LIMIT fills at exact price
        else:
            # MARKET fill at current price + slippage
            fill_price = current

        # Slippage
        vol_ratio = 1.0  # Could adjust based on market conditions
        slippage_bps = random.uniform(_SLIPPAGE_BPS_BASE, _SLIPPAGE_BPS_SPIKE * vol_ratio)
        slippage_pct = slippage_bps / 10_000

        if signal.direction == "LONG":
            fill_price *= (1 + slippage_pct)
        else:
            fill_price *= (1 - slippage_pct)

        return fill_price

    # ─── Heartbeat loop ──────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Check all open positions every HEARTBEAT_INTERVAL seconds."""
        logger.info("Heartbeat started (1-second TP/SL checking)")
        while not self._shutdown:
            try:
                await self._check_all_positions()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"Heartbeat error:\n{traceback.format_exc()}")
            await asyncio.sleep(self._cfg.HEARTBEAT_INTERVAL)

    async def _check_all_positions(self) -> None:
        if not self._positions:
            return

        for pos_id, pos in list(self._positions.items()):
            # Check OPEN and TP1_HIT — TP1_HIT still needs TP2/SL-at-breakeven monitoring
            if pos.status not in ("OPEN", "TP1_HIT"):
                continue

            try:
                current = self._de.get_current_price(pos.symbol)
                if current < 1e-10:
                    continue

                await self._check_position(pos, current)

            except Exception:
                logger.error(f"Position check error [{pos_id}]:\n{traceback.format_exc()}")

    async def _check_position(self, pos: Position, current: float) -> None:
        now = datetime.now(timezone.utc)

        # Update unrealized PnL
        pos.unrealized_pnl = pos.calc_unrealized_pnl(current)

        # Track MFE / MAE
        if pos.direction == "LONG":
            pos.max_favorable_excursion = max(pos.max_favorable_excursion, current)
            pos.max_adverse_excursion = min(pos.max_adverse_excursion or current, current)
        else:
            pos.max_favorable_excursion = min(pos.max_favorable_excursion or current, current)
            pos.max_adverse_excursion = max(pos.max_adverse_excursion, current)

        close_reason: Optional[str] = None
        close_price: float = current

        # ── Expiry ────────────────────────────────────────────
        if pos.expiry and now > pos.expiry:
            close_reason = "EXPIRED"
            close_price = current

        # ── Stop Loss ─────────────────────────────────────────
        elif pos.direction == "LONG" and current <= pos.sl_price:
            close_reason = "SL"
            close_price = pos.sl_price  # Assume fills at SL (conservative)

        elif pos.direction == "SHORT" and current >= pos.sl_price:
            close_reason = "SL"
            close_price = pos.sl_price

        # ── Take Profit 1 ─────────────────────────────────────
        elif pos.direction == "LONG" and current >= pos.tp1_price and not pos.tp1_hit:
            pos.tp1_hit = True
            if pos.tp2_price:
                # Partial close: close 50% at TP1, let rest run to TP2
                # Move SL to breakeven
                pos.sl_price = pos.entry_price
                logger.info(
                    f"TP1 HIT | {pos.symbol} | Price: {current:.4f} | "
                    f"SL moved to breakeven | TP2: {pos.tp2_price:.4f}"
                )
                pos.status = "TP1_HIT"
                await self._db.update_position(pos)
                # Record partial close (half size)
                partial_pnl = (close_price - pos.entry_price) * (pos.size * 0.5)
                await self._record_trade(pos, current, "TP1", partial_pnl, 0.5)
                return
            else:
                close_reason = "TP1"
                close_price = pos.tp1_price

        elif pos.direction == "SHORT" and current <= pos.tp1_price and not pos.tp1_hit:
            pos.tp1_hit = True
            if pos.tp2_price:
                pos.sl_price = pos.entry_price
                logger.info(
                    f"TP1 HIT | {pos.symbol} | Price: {current:.4f} | "
                    f"SL moved to breakeven"
                )
                pos.status = "TP1_HIT"
                await self._db.update_position(pos)
                partial_pnl = (pos.entry_price - close_price) * (pos.size * 0.5)
                await self._record_trade(pos, current, "TP1", partial_pnl, 0.5)
                return
            else:
                close_reason = "TP1"
                close_price = pos.tp1_price

        # ── Take Profit 2 (after TP1 hit) ────────────────────
        elif pos.tp2_price and pos.tp1_hit:
            if pos.direction == "LONG" and current >= pos.tp2_price:
                close_reason = "TP2"
                close_price = pos.tp2_price
            elif pos.direction == "SHORT" and current <= pos.tp2_price:
                close_reason = "TP2"
                close_price = pos.tp2_price
            # Also check SL hit on TP1_HIT positions (now at breakeven)
            elif pos.direction == "LONG" and current <= pos.sl_price:
                close_reason = "SL"
                close_price = pos.sl_price
            elif pos.direction == "SHORT" and current >= pos.sl_price:
                close_reason = "SL"
                close_price = pos.sl_price

        if close_reason:
            await self._close_position(pos, close_price, close_reason)

    async def _close_position(self, pos: Position, close_price: float, reason: str) -> None:
        now = datetime.now(timezone.utc)
        duration = (now - pos.open_time).total_seconds()

        if pos.direction == "LONG":
            pnl = (close_price - pos.entry_price) * pos.size
        else:
            pnl = (pos.entry_price - close_price) * pos.size

        # If TP1 was already taken (half closed), remaining half:
        if pos.tp1_hit and pos.tp2_price:
            remaining_size = pos.size * 0.5
            if pos.direction == "LONG":
                pnl = (close_price - pos.entry_price) * remaining_size
            else:
                pnl = (pos.entry_price - close_price) * remaining_size

        sl_dist = abs(pos.entry_price - pos.sl_price)
        r_multiple = (pnl / pos.size / sl_dist) if sl_dist > 1e-10 and pos.size > 1e-10 else 0.0

        pos.pnl = pnl
        pos.status = reason if reason in ("TP1", "TP2") else reason
        pos.close_time = now
        pos.close_price = close_price

        # Update DB
        await self._db.update_position(pos)
        del self._positions[pos.id]

        logger.info(
            f"POSITION CLOSED | {pos.symbol} {pos.direction} | "
            f"{reason} @ {close_price:.4f} | "
            f"PnL: {pnl:+.4f} | R: {r_multiple:+.2f} | "
            f"Duration: {duration/3600:.1f}h"
        )

        result = TradeResult(
            position_id=pos.id,
            symbol=pos.symbol,
            strategy=pos.strategy,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=close_price,
            size=pos.size,
            pnl=pnl,
            exit_reason=reason,
            duration_seconds=duration,
            r_multiple=r_multiple,
            confidence=pos.confidence,
        )

        await self._db.insert_trade_result(result)
        await self._rm.on_trade_closed(result)

        for cb in self._on_trade_closed_cbs:
            asyncio.create_task(cb(result))

    async def _record_trade(self, pos: Position, price: float,
                             reason: str, pnl: float, size_fraction: float) -> None:
        """Record partial close (TP1 partial)."""
        sl_dist = abs(pos.entry_price - pos.sl_price)
        effective_size = pos.size * size_fraction
        r = (pnl / effective_size / sl_dist) if sl_dist > 1e-10 and effective_size > 1e-10 else 0.0
        duration = (datetime.now(timezone.utc) - pos.open_time).total_seconds()

        result = TradeResult(
            position_id=pos.id,
            symbol=pos.symbol,
            strategy=pos.strategy,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=price,
            size=effective_size,
            pnl=pnl,
            exit_reason=f"{reason}_PARTIAL",
            duration_seconds=duration,
            r_multiple=r,
            confidence=pos.confidence,
        )
        await self._db.insert_trade_result(result)
        await self._rm.on_trade_closed(result)
        for cb in self._on_trade_closed_cbs:
            asyncio.create_task(cb(result))

    # ─── Load/persist ─────────────────────────────────────────

    async def _load_positions(self) -> None:
        rows = await self._db.get_open_positions()
        for row in rows:
            pos = Position(
                id=row["id"],
                symbol=row["symbol"],
                direction=row["direction"],
                entry_price=float(row["entry_price"]),
                size=float(row["size"]),
                sl_price=float(row["sl_price"]),
                tp1_price=float(row["tp1_price"]),
                tp2_price=float(row["tp2_price"]) if row["tp2_price"] else None,
                open_time=datetime.fromtimestamp(row["open_time"] / 1000, tz=timezone.utc),
                strategy=row["strategy"],
                confidence=float(row["confidence"]),
                status=row["status"],
                pnl=float(row["pnl"]),
                tp1_hit=bool(row["tp1_hit"]),
                expiry=datetime.fromtimestamp(
                    row["expiry"] / 1000, tz=timezone.utc
                ) if row["expiry"] else None,
            )
            self._positions[pos.id] = pos
        logger.info(f"Loaded {len(self._positions)} open positions from DB")

    # ─── Queries ─────────────────────────────────────────────

    def get_open_positions(self) -> List[Position]:
        return [p for p in self._positions.values() if p.status == "OPEN" or p.status == "TP1_HIT"]

    def get_position_count(self) -> int:
        return len([p for p in self._positions.values() if p.is_open])

    async def close_all_positions(self, reason: str = "MANUAL") -> None:
        """Emergency close all open positions."""
        for pos in list(self._positions.values()):
            if pos.is_open:
                price = self._de.get_current_price(pos.symbol)
                if price > 0:
                    await self._close_position(pos, price, reason)
        logger.warning(f"All positions closed: {reason}")
