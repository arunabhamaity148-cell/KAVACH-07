"""
KAVACH-07 — Risk Manager
Position sizing, circuit breakers, drawdown controls, balance persistence.
All state survives restarts via SQLite.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from config import Config
from database import Database
from models import Position, RiskMetrics, Signal, TradeResult
from utils import get_logger

logger = get_logger(__name__)

# Circuit breaker states
_CB_OK     = "OK"
_CB_REDUCE = "REDUCE"
_CB_HALT   = "HALT"

# Drawdown adjustments
_DD_LEVELS = [
    (0.05, 0.75),   # 5% DD → 75% size
    (0.10, 0.50),   # 10% DD → 50% size
    (0.15, 0.00),   # 15% DD → HALT
]


class RiskManager:

    def __init__(self, config: Config, db: Database):
        self._cfg = config
        self._db = db
        self._metrics = RiskMetrics(
            balance=config.INITIAL_BALANCE,
            peak_balance=config.INITIAL_BALANCE,
            daily_start_balance=config.INITIAL_BALANCE,
        )
        self._open_exposure: float = 0.0   # Sum of (risk_pct × balance) for open positions
        self._lock = asyncio.Lock()
        self._save_task: Optional[asyncio.Task] = None
        self._on_halt_cb = None  # Callable for circuit breaker Telegram alerts

    # ─── Lifecycle ───────────────────────────────────────────

    async def load(self) -> None:
        """Load saved state from DB."""
        row = await self._db.load_risk_metrics()
        if row:
            m = self._metrics
            m.balance              = float(row.get("balance", m.balance))
            m.peak_balance         = float(row.get("peak_balance", m.peak_balance))
            m.total_pnl            = float(row.get("total_pnl", 0))
            m.gross_profit         = float(row.get("gross_profit", 0))
            m.gross_loss           = float(row.get("gross_loss", 0))
            m.total_trades         = int(row.get("total_trades", 0))
            m.winning_trades       = int(row.get("winning_trades", 0))
            m.losing_trades        = int(row.get("losing_trades", 0))
            m.consecutive_losses   = int(row.get("consecutive_losses", 0))
            m.consecutive_wins     = int(row.get("consecutive_wins", 0))
            m.total_signals        = int(row.get("total_signals", 0))
            m.daily_pnl            = float(row.get("daily_pnl", 0))
            m.daily_start_balance  = float(row.get("daily_start_balance", m.balance))
            m.circuit_state        = row.get("circuit_state", _CB_OK)
            m.circuit_reason       = row.get("circuit_reason", "")
            m.halt_until           = row.get("halt_until")
            m.paused               = bool(row.get("paused", 0))

            # Recalculate drawdown
            if m.peak_balance > 0:
                m.drawdown = max(0.0, (m.peak_balance - m.balance) / m.peak_balance)

            logger.info(
                f"Risk state loaded: balance=${m.balance:.2f}, "
                f"drawdown={m.drawdown*100:.1f}%, "
                f"trades={m.total_trades}"
            )
        else:
            logger.info(f"No saved risk state — starting fresh: ${self._cfg.INITIAL_BALANCE:.2f}")

    async def start_persistence(self) -> None:
        """Start periodic save task."""
        self._save_task = asyncio.create_task(self._persist_loop(), name="risk_persist")

    async def stop(self) -> None:
        if self._save_task:
            self._save_task.cancel()
            await asyncio.gather(self._save_task, return_exceptions=True)
        await self._db.save_risk_metrics(self._metrics)
        logger.info("RiskManager state saved")

    # ─── Position Sizing ─────────────────────────────────────

    def calculate_size(self, signal: Signal) -> Tuple[float, str]:
        """
        Returns (size_in_base_asset, rejection_reason).
        size=0 means rejected.
        """
        m = self._metrics

        # ── Circuit breaker check ─────────────────────────────
        if m.circuit_state == _CB_HALT:
            if m.halt_until and time.time() < m.halt_until:
                remaining = int(m.halt_until - time.time())
                return 0.0, f"HALTED for {remaining}s: {m.circuit_reason}"
            else:
                # Auto-resume after halt expires
                m.circuit_state = _CB_OK
                m.circuit_reason = ""
                m.halt_until = None
                logger.info("Circuit breaker auto-resumed after halt period")

        if m.paused:
            return 0.0, "System paused"

        # ── Maximum total exposure check ──────────────────────
        risk_amount = m.balance * signal.risk_pct
        if (self._open_exposure + risk_amount) > (m.balance * self._cfg.MAX_TOTAL_EXPOSURE):
            return 0.0, f"Total exposure limit reached ({self._cfg.MAX_TOTAL_EXPOSURE*100:.1f}%)"

        # ── Base size calculation ─────────────────────────────
        sl_distance = abs(signal.entry_price - signal.sl_price)
        if sl_distance < 1e-10:
            return 0.0, "SL distance too small"

        base_risk = m.balance * signal.risk_pct
        raw_size = base_risk / sl_distance

        # ── Drawdown adjustment ───────────────────────────────
        dd_mult = self._drawdown_multiplier(m.drawdown)
        if dd_mult <= 0:
            return 0.0, f"Drawdown too deep: {m.drawdown*100:.1f}%"

        # ── Circuit breaker size reduction ────────────────────
        cb_mult = 0.5 if m.circuit_state == _CB_REDUCE else 1.0

        # ── ATR volatility adjustment ─────────────────────────
        # Higher ATR → already reflected in wider SL → size naturally reduces
        # Additional vol adjustment: cap size if ATR is extreme
        atr_pct = signal.atr / signal.entry_price if signal.entry_price > 0 else 0
        atr_mult = max(0.5, min(1.5, 0.01 / max(atr_pct, 0.001)))  # target 1% ATR

        final_size = raw_size * dd_mult * cb_mult

        # Sanity cap: never risk more than MAX_RISK_PER_TRADE regardless of adjustments
        max_risk_size = (m.balance * self._cfg.MAX_RISK_PER_TRADE) / sl_distance
        final_size = min(final_size, max_risk_size)

        if final_size < 1e-8:
            return 0.0, "Calculated size too small"

        return round(final_size, 6), ""

    def _drawdown_multiplier(self, drawdown: float) -> float:
        for threshold, multiplier in sorted(_DD_LEVELS, key=lambda x: x[0], reverse=True):
            if drawdown >= threshold:
                return multiplier
        return 1.0

    # ─── Position lifecycle callbacks ─────────────────────────

    def on_position_opened(self, pos: Position) -> None:
        """Reserve exposure when position opens."""
        async def _inner():
            async with self._lock:
                risk = self._metrics.balance * (pos.confidence * self._cfg.MAX_RISK_PER_TRADE)
                self._open_exposure += risk

        asyncio.create_task(_inner())

    async def on_trade_closed(self, result: TradeResult) -> None:
        """Update balance and metrics when trade closes."""
        async with self._lock:
            m = self._metrics
            m.balance += result.pnl
            m.total_pnl += result.pnl
            m.total_trades += 1

            if result.pnl > 0:
                m.winning_trades += 1
                m.gross_profit += result.pnl
                m.consecutive_losses = 0
                m.consecutive_wins += 1
            else:
                m.losing_trades += 1
                m.gross_loss += result.pnl
                m.consecutive_wins = 0
                m.consecutive_losses += 1

            m.daily_pnl = m.balance - m.daily_start_balance

            # Peak & drawdown
            if m.balance > m.peak_balance:
                m.peak_balance = m.balance
            m.drawdown = max(0.0, (m.peak_balance - m.balance) / m.peak_balance)

            # Release exposure
            risk_estimate = abs(result.entry_price - result.exit_price) * result.size
            self._open_exposure = max(0.0, self._open_exposure - risk_estimate)

            # Run circuit breakers
            self._run_circuit_breakers()

            # Save to DB immediately on trade close
            await self._db.save_risk_metrics(m)

        logger.info(
            f"Balance: ${m.balance:.2f} | "
            f"PnL: {result.pnl:+.2f} | "
            f"Drawdown: {m.drawdown*100:.1f}% | "
            f"WR: {m.win_rate*100:.0f}% ({m.total_trades} trades)"
        )

    def increment_signals(self) -> None:
        self._metrics.total_signals += 1

    # ─── Circuit Breakers ─────────────────────────────────────

    def _run_circuit_breakers(self) -> None:
        m = self._metrics

        if m.circuit_state == _CB_HALT:
            return  # Already halted

        # Consecutive losses halt (1 hour)
        if m.consecutive_losses >= 3:
            self._trigger_halt(
                f"3 consecutive losses ({m.consecutive_losses})",
                duration_s=3600,
            )
            return

        # Daily loss limit halt (until midnight)
        daily_loss_pct = -m.daily_pnl / m.daily_start_balance if m.daily_start_balance > 0 else 0
        if daily_loss_pct >= self._cfg.MAX_DAILY_LOSS:
            midnight = self._next_midnight()
            self._trigger_halt(
                f"Daily loss limit ({self._cfg.MAX_DAILY_LOSS*100:.0f}%): "
                f"-{daily_loss_pct*100:.1f}% today",
                until=midnight,
            )
            return

        # Deep drawdown halt (manual reset required)
        if m.drawdown >= self._cfg.DRAWDOWN_HALT_THRESHOLD:
            self._trigger_halt(
                f"Drawdown halt ({self._cfg.DRAWDOWN_HALT_THRESHOLD*100:.0f}%): "
                f"current={m.drawdown*100:.1f}%",
                duration_s=86_400 * 7,  # 7 days — requires manual /resume
            )
            return

        # Moderate drawdown: reduce size
        if m.drawdown >= self._cfg.DRAWDOWN_REDUCE_THRESHOLD:
            if m.circuit_state != _CB_REDUCE:
                m.circuit_state = _CB_REDUCE
                m.circuit_reason = (
                    f"Drawdown {m.drawdown*100:.1f}% ≥ "
                    f"{self._cfg.DRAWDOWN_REDUCE_THRESHOLD*100:.0f}% → 50% size"
                )
                logger.warning(f"CIRCUIT BREAKER: {m.circuit_reason}")
        else:
            # Conditions improved
            if m.circuit_state == _CB_REDUCE:
                m.circuit_state = _CB_OK
                m.circuit_reason = ""
                logger.info("Circuit breaker REDUCE cleared")

    def _trigger_halt(self, reason: str, duration_s: float = 0,
                       until: Optional[float] = None) -> None:
        m = self._metrics
        if until is None:
            until = time.time() + duration_s
        m.circuit_state = _CB_HALT
        m.circuit_reason = reason
        m.halt_until = until
        hours = (until - time.time()) / 3600
        logger.critical(f"CIRCUIT BREAKER HALT: {reason} | Duration: {hours:.1f}h")
        # Fire Telegram alert if wired
        if self._on_halt_cb:
            asyncio.create_task(self._on_halt_cb(_CB_HALT, reason))

    def register_halt_callback(self, cb) -> None:
        """Register a coroutine callback (state, reason) for circuit breaker events."""
        self._on_halt_cb = cb

    @staticmethod
    def _next_midnight() -> float:
        """UTC timestamp of next midnight. Uses timedelta — safe on any month-end day."""
        now = datetime.now(timezone.utc)
        # Truncate to today midnight, then add 1 day — never fails (no day+1 arithmetic)
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return (today_midnight + timedelta(days=1)).timestamp()

    # ─── Daily reset ─────────────────────────────────────────

    async def daily_reset(self) -> None:
        """Reset daily P&L tracking at UTC midnight."""
        async with self._lock:
            self._metrics.daily_start_balance = self._metrics.balance
            self._metrics.daily_pnl = 0.0
        logger.info(f"Daily reset — new start balance: ${self._metrics.balance:.2f}")

    # ─── Persistence ─────────────────────────────────────────

    async def _persist_loop(self) -> None:
        """Save risk state every 60 seconds."""
        while True:
            try:
                await asyncio.sleep(60)
                async with self._lock:
                    await self._db.save_risk_metrics(self._metrics)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Risk persist error: {e}")

    # ─── Pause / Resume / Halt ───────────────────────────────

    def pause(self) -> None:
        self._metrics.paused = True
        logger.info("Trading paused by command")

    def resume(self) -> None:
        m = self._metrics
        m.paused = False
        # Also clear soft halts when resuming
        if m.circuit_state == _CB_HALT:
            m.circuit_state = _CB_OK
            m.circuit_reason = ""
            m.halt_until = None
        logger.info("Trading resumed by command")

    def halt(self) -> None:
        self._trigger_halt("Emergency halt by operator", duration_s=86_400 * 7)

    # ─── Accessors ───────────────────────────────────────────

    @property
    def metrics(self) -> RiskMetrics:
        return self._metrics

    @property
    def balance(self) -> float:
        return self._metrics.balance

    @property
    def is_halted(self) -> bool:
        m = self._metrics
        if m.paused:
            return True
        if m.circuit_state == _CB_HALT:
            if m.halt_until and time.time() > m.halt_until:
                m.circuit_state = _CB_OK
                return False
            return True
        return False
