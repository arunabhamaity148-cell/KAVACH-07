"""
KAVACH-07 — Monitoring Engine
Health checks, metrics aggregation, hourly reports.
Tracks memory, WS liveness, signal flow, error rate.
"""
from __future__ import annotations

import asyncio
import os
import time
import traceback
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable

from config import Config
from data_engine import DataEngine
from database import Database
from ml_engine import MLEngine
from models import HealthStatus
from risk_manager import RiskManager
from utils import get_logger

logger = get_logger(__name__)

_WS_STALE_SECONDS   = 120   # No WS msg for 2 min → unhealthy
_DATA_STALE_SECONDS = 60    # Data not updated for 60s → unhealthy
_SIGNAL_STALE_HOURS = 4     # FIX: 2h → 4h  (বাজার quiet থাকলে 2h signal না আসতেই পারে)
_HEALTH_INTERVAL    = 30    # Health check every 30 seconds
_REPORT_INTERVAL    = 3600  # Hourly report

# FIX: debounce constant গুলো class-level এ তুলে আনা হয়েছে
# আগে __init__ এর ভেতরে local variable ছিল — ফলে _run_health_check() এ দেখাই যেত না!
# সেজন্যই debounce কাজ করছিল না এবং প্রতি ৩০ সেকেন্ডে alert যাচ্ছিল।
_WS_ALERT_DEBOUNCE     = 300   # min 5 min between WS stale alerts
_SIGNAL_ALERT_DEBOUNCE = 3600  # FIX: 30min → 60min between no-signal alerts


class MonitoringEngine:

    def __init__(self, config: Config, data_engine: DataEngine,
                 db: Database, risk_manager: RiskManager, ml_engine: MLEngine):
        self._cfg = config
        self._de = data_engine
        self._db = db
        self._rm = risk_manager
        self._ml = ml_engine

        self._health = HealthStatus()
        self._start_time = time.time()

        # Error tracking
        self._error_count = 0

        # Signal tracking
        self._last_signal_time: float = time.time()

        # FIX: debounce tracker গুলো instance variable হিসেবে রাখা হয়েছে
        # (আগে __init__-এ local variable ছিল, তাই _run_health_check-এ access হতো না)
        self._last_ws_alert_time: float = 0.0
        self._last_signal_alert_time: float = 0.0

        # Circuit breaker change detection
        self._last_circuit_state: str = "OK"

        # Callbacks
        self._on_alert_cbs: list = []

        self._tasks: list = []
        self._shutdown = False

    # ─── Lifecycle ───────────────────────────────────────────

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._health_loop(), name="health_loop"),
            asyncio.create_task(self._report_loop(), name="report_loop"),
            asyncio.create_task(self._midnight_reset_loop(), name="midnight_reset"),
            asyncio.create_task(self._cleanup_loop(), name="db_cleanup"),
        ]
        logger.info("MonitoringEngine started")

    async def stop(self) -> None:
        self._shutdown = True
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def on_alert(self, cb: Callable[[str], Awaitable[None]]) -> None:
        self._on_alert_cbs.append(cb)

    def notify_signal(self) -> None:
        self._last_signal_time = time.time()

    def notify_error(self, component: str, msg: str, tb: str = "") -> None:
        self._error_count += 1
        self._health.error_count = self._error_count
        asyncio.create_task(self._db.log_error(component, msg, tb))

    # ─── Health Check ─────────────────────────────────────────

    async def _health_loop(self) -> None:
        while not self._shutdown:
            try:
                await self._run_health_check()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(_HEALTH_INTERVAL)

    async def _run_health_check(self) -> None:
        h = self._health
        now = time.time()

        h.ws_alive        = (now - self._de.last_ws_msg) < _WS_STALE_SECONDS
        h.data_fresh      = h.ws_alive   # Data freshness tied to WS liveness
        h.signals_flowing = (now - self._last_signal_time) < _SIGNAL_STALE_HOURS * 3600
        h.no_errors       = self._error_count < 20
        h.uptime_seconds  = now - self._start_time
        h.ws_reconnects   = self._de.ws_reconnects
        h.error_count     = self._error_count

        # Memory tracking
        try:
            import resource
            mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # MB
            h.memory_mb = mem
            if mem > 500:
                logger.warning(f"Memory usage high: {mem:.0f} MB")
        except Exception:
            pass

        # Alert on problems — debounced to prevent Telegram spam
        # FIX: এখন module-level constant ব্যবহার করা হচ্ছে (_WS_ALERT_DEBOUNCE, _SIGNAL_ALERT_DEBOUNCE)
        # এবং self._last_*_alert_time সঠিকভাবে update হচ্ছে
        if not h.ws_alive:
            if (now - self._last_ws_alert_time) > _WS_ALERT_DEBOUNCE:
                self._last_ws_alert_time = now
                await self._send_alert("⚠️ WebSocket connection stale — data may be delayed")
        elif not h.signals_flowing:
            if (now - self._last_signal_alert_time) > _SIGNAL_ALERT_DEBOUNCE:
                self._last_signal_alert_time = now
                elapsed_h = (now - self._last_signal_time) / 3600
                await self._send_alert(
                    f"⚠️ No signals in {elapsed_h:.1f}h — check strategy conditions"
                )

        # Circuit breaker state change → immediate Telegram alert
        current_circuit = self._rm.metrics.circuit_state
        if current_circuit != self._last_circuit_state:
            self._last_circuit_state = current_circuit
            if current_circuit != "OK":
                reason = self._rm.metrics.circuit_reason
                await self._send_alert(
                    f"🚨 <b>CIRCUIT BREAKER: {current_circuit}</b>\n{reason}"
                )

        logger.debug(
            f"Health: WS={'✅' if h.ws_alive else '❌'} "
            f"Data={'✅' if h.data_fresh else '❌'} "
            f"Signals={'✅' if h.signals_flowing else '⚠️'} "
            f"Errors={h.error_count} "
            f"Mem={h.memory_mb:.0f}MB "
            f"Reconnects={h.ws_reconnects}"
        )

    # ─── Hourly Report ────────────────────────────────────────

    async def _report_loop(self) -> None:
        # Wait until the next hour boundary
        now = time.time()
        next_hour = ((now // 3600) + 1) * 3600
        await asyncio.sleep(next_hour - now)

        while not self._shutdown:
            try:
                report = await self.build_hourly_report()
                await self._send_alert(report)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"Report loop error:\n{traceback.format_exc()}")
            await asyncio.sleep(_REPORT_INTERVAL)

    async def build_hourly_report(self) -> str:
        """Build the hourly performance report string."""
        now = datetime.now(timezone.utc)
        m = self._rm.metrics
        h = self._health
        ml = self._ml.stats

        # Recent trades (last hour)
        trades = await self._db.get_recent_trades(limit=50)
        hour_cutoff = time.time() * 1000 - 3_600_000
        hour_trades = [t for t in trades if t.get("timestamp", 0) > hour_cutoff]

        # Strategy breakdown
        strategy_stats = await self._db.get_strategy_stats()

        # Top performers this session
        best_trade = max(trades, key=lambda t: t.get("pnl", 0), default=None) if trades else None
        worst_trade = min(trades, key=lambda t: t.get("pnl", 0), default=None) if trades else None

        lines = [
            f"📊 KAVACH-07 HOURLY — {now.strftime('%H:%M')} UTC",
            "━" * 32,
            "",
            f"🎯 Signals: {m.total_signals} | Trades: {m.total_trades} | Wins: {m.winning_trades}",
            f"📈 Win Rate: {m.win_rate*100:.0f}% | PF: {m.profit_factor:.2f}",
            f"📉 Drawdown: {m.drawdown*100:.1f}% | Balance: ${m.balance:.2f}",
            "",
        ]

        # Best/worst
        if best_trade:
            lines.append(
                f"🔥 Best: {best_trade['symbol']} "
                f"{best_trade['pnl']:+.2f} ({best_trade['strategy']})"
            )
        if worst_trade and worst_trade != best_trade:
            lines.append(
                f"❌ Worst: {worst_trade['symbol']} "
                f"{worst_trade['pnl']:+.2f} ({worst_trade['strategy']})"
            )

        lines.append("")

        # Strategy breakdown (top 3)
        if strategy_stats:
            lines.append("📊 Strategy Stats:")
            for s in strategy_stats[:3]:
                wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
                lines.append(
                    f"  {s['strategy']}: {s['trades']}t | "
                    f"WR={wr:.0f}% | PnL={s['total_pnl']:+.2f}"
                )
            lines.append("")

        # System health
        ws_icon  = "✅" if h.ws_alive else "❌"
        data_icon = "✅" if h.data_fresh else "❌"
        ml_icon  = "✅" if ml["model_available"] else "❌"

        lines += [
            f"📡 WS: {ws_icon} | Data: {data_icon} | ML: {ml_icon}",
            f"⚠️ Errors: {h.error_count} | "
            f"Reconnects: {h.ws_reconnects} | "
            f"Mem: {h.memory_mb:.0f}MB",
            f"🤖 ML: {ml['samples_seen']} samples | "
            f"ROC-AUC: {ml['roc_auc']:.3f} | "
            f"Drift: {'Yes' if ml['drift_detections'] > 0 else 'No'}",
            "",
            f"⏳ Next Signal: Scanning...",
        ]

        # Circuit breaker warning
        if m.circuit_state != "OK":
            lines.insert(2, f"🚨 CIRCUIT: {m.circuit_state} — {m.circuit_reason}")

        return "\n".join(lines)

    async def build_status_message(self) -> str:
        """Short status message for /status command."""
        m = self._rm.metrics
        h = self._health
        now = datetime.now(timezone.utc)
        uptime_h = h.uptime_seconds / 3600

        return (
            f"⚙️ KAVACH-07 STATUS — {now.strftime('%H:%M:%S')} UTC\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance: ${m.balance:.2f}\n"
            f"📈 PnL: {m.total_pnl:+.2f} | Daily: {m.daily_pnl:+.2f}\n"
            f"📉 Drawdown: {m.drawdown*100:.1f}%\n"
            f"🔄 Trades: {m.total_trades} | WR: {m.win_rate*100:.0f}%\n"
            f"📡 WS: {'✅' if h.ws_alive else '❌'} | "
            f"Errors: {h.error_count}\n"
            f"⏱️ Uptime: {uptime_h:.1f}h\n"
            f"🔒 Circuit: {m.circuit_state}"
        )

    # ─── Midnight reset ───────────────────────────────────────

    async def _midnight_reset_loop(self) -> None:
        """Reset daily metrics at UTC midnight."""
        while not self._shutdown:
            now = time.time()
            next_midnight = ((now // 86_400) + 1) * 86_400
            await asyncio.sleep(next_midnight - now)
            if not self._shutdown:
                await self._rm.daily_reset()
                logger.info("Daily reset executed at midnight UTC")

    # ─── DB Cleanup ──────────────────────────────────────────

    async def _cleanup_loop(self) -> None:
        """Clean old data from DB daily."""
        while not self._shutdown:
            await asyncio.sleep(86_400)
            try:
                await self._db.cleanup_old_data(days=30)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"DB cleanup error: {e}")

    # ─── Alert dispatch ───────────────────────────────────────

    async def _send_alert(self, message: str) -> None:
        for cb in self._on_alert_cbs:
            try:
                asyncio.create_task(cb(message))
            except Exception:
                pass

    # ─── Accessors ───────────────────────────────────────────

    @property
    def health(self) -> HealthStatus:
        return self._health
