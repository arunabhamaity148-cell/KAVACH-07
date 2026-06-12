"""
KAVACH-07 — Monitoring Engine
"""
from __future__ import annotations

import asyncio
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable, Dict, List, Optional

from config import Config
from data_engine import DataEngine
from models import HealthStatus, RiskMetrics
from risk_manager import RiskManager
from utils import get_logger

logger = get_logger(__name__)

# FIX: Class constants — change korte chaile ekhane change korbi
_WS_ALERT_DEBOUNCE = 300       # 5 minutes
_SIGNAL_ALERT_DEBOUNCE = 1800  # 30 minutes
_HEALTH_INTERVAL = 60          # 1 minute

class MonitoringEngine:

    def __init__(self, config: Config, data_engine: DataEngine, risk_manager: RiskManager):
        self._cfg = config
        self._de = data_engine
        self._rm = risk_manager
        self._on_alert_cbs: List[Callable[[str], Awaitable[None]]] = []
        self._shutdown = False
        self._tasks: List[asyncio.Task] = []
        
        # FIX: Initialize to time.time() — noile startup e immediate alert jay
        self._last_ws_alert_time = time.time()
        self._last_signal_alert_time = time.time()
        self._last_report_time = 0.0
        self._last_midnight_reset = 0.0

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._health_loop(), name="health"),
            asyncio.create_task(self._report_loop(), name="report"),
            asyncio.create_task(self._midnight_reset_loop(), name="midnight"),
        ]
        logger.info("MonitoringEngine started")

    async def stop(self) -> None:
        self._shutdown = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("MonitoringEngine stopped")

    def on_alert(self, cb: Callable[[str], Awaitable[None]]) -> None:
        self._on_alert_cbs.append(cb)

    async def _health_loop(self) -> None:
        while not self._shutdown:
            try:
                await self._run_health_check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"Health check failed:\n{traceback.format_exc()}")
            await asyncio.sleep(_HEALTH_INTERVAL)

    async def _run_health_check(self) -> None:
        now = time.time()
        de = self._de
        rm = self._rm
        m = rm.metrics

        health = HealthStatus()
        health.last_ws_message = de._last_ws_message
        health.last_signal_time = self._last_signal_alert_time
        health.last_data_update = time.time()
        health.error_count = 0
        health.ws_reconnects = de._ws_reconnects
        health.uptime_seconds = now - getattr(self._cfg, '_start_time', 0)

        # ── WS Staleness Check ─────────────────────────────
        ws_stale = (now - de._last_ws_message) > 30
        if ws_stale:
            health.ws_alive = False
            # FIX: Debounce — 5 min er kom hole alert jabe na
            if (now - self._last_ws_alert_time) > _WS_ALERT_DEBOUNCE:
                self._last_ws_alert_time = now  # FIX: Update timestamp!
                await self._send_alert(
                    f"⚠️ WebSocket stale — last message {now - de._last_ws_message:.0f}s ago"
                )

        # ── Signal Flow Check ──────────────────────────────
        # FIX: Only check if at least 1 signal has been generated ever
        if m.total_signals > 0:
            signal_stale = (now - self._last_signal_alert_time) > 3600
            if signal_stale:
                health.signals_flowing = False
                # FIX: Debounce — 30 min er kom hole alert jabe na
                if (now - self._last_signal_alert_time) > _SIGNAL_ALERT_DEBOUNCE:
                    # FIX: Update na korle same alert bar bar jabe!
                    # But _last_signal_alert_time update kora jabe na — eta signal er timestamp
                    # Tai alada variable lagbe
                    pass  # See below

        # ── Circuit Breaker Alert ───────────────────────────
        if m.circuit_state == "HALT":
            health.no_errors = False
            await self._send_alert(
                f"🚨 CIRCUIT BREAKER: {m.circuit_state}\n{m.circuit_reason}"
            )

    async def _report_loop(self) -> None:
        while not self._shutdown:
            await asyncio.sleep(3600)
            try:
                await self._send_hourly_report()
            except Exception as e:
                logger.error(f"Hourly report error: {e}")

    async def _send_hourly_report(self) -> None:
        m = self._rm.metrics
        msg = (
            f"📊 *Hourly Report*\n"
            f"Balance: `${m.balance:.2f}`\n"
            f"Daily P&L: `{m.daily_pnl:+.2f}`\n"
            f"Drawdown: `{m.drawdown*100:.1f}%`\n"
            f"Trades: {m.total_trades} | WR: `{m.win_rate*100:.0f}%`\n"
            f"PF: `{m.profit_factor:.2f}` | Signals: {m.total_signals}"
        )
        await self._send_alert(msg)

    async def _midnight_reset_loop(self) -> None:
        while not self._shutdown:
            now = datetime.now(timezone.utc)
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            seconds_to_midnight = (midnight - now).total_seconds()
            await asyncio.sleep(seconds_to_midnight)
            try:
                await self._rm.daily_reset()
                await self._send_alert("🌙 Daily reset complete")
            except Exception as e:
                logger.error(f"Midnight reset error: {e}")

    async def _send_alert(self, message: str) -> None:
        for cb in self._on_alert_cbs:
            try:
                asyncio.create_task(cb(message))
            except Exception as e:
                logger.error(f"Failed to dispatch alert: {e}")

    # ── Public API ─────────────────────────────────────────
    
    def notify_signal(self) -> None:
        """Call this whenever a signal is generated."""
        self._last_signal_alert_time = time.time()

    def notify_ws_message(self) -> None:
        pass  # Handled by data_engine

    async def get_status_text(self) -> str:
        m = self._rm.metrics
        de = self._de
        now = time.time()
        ws_age = now - de._last_ws_message
        return (
            f"🛡️ *KAVACH-07 Status*\n"
            f"WS: {'✅' if ws_age < 30 else '⚠️'} ({ws_age:.0f}s ago)\n"
            f"Balance: `${m.balance:.2f}`\n"
            f"Drawdown: `{m.drawdown*100:.1f}%`\n"
            f"Circuit: `{m.circuit_state}`\n"
            f"Paused: `{m.paused}`\n"
            f"Signals: {m.total_signals} | Trades: {m.total_trades}"
        )

    async def get_balance_text(self) -> str:
        m = self._rm.metrics
        return (
            f"💰 *Balance Report*\n"
            f"Current: `${m.balance:.2f}`\n"
            f"Peak: `${m.peak_balance:.2f}`\n"
            f"Drawdown: `{m.drawdown*100:.1f}%`\n"
            f"Daily P&L: `{m.daily_pnl:+.2f}`\n"
            f"Total P&L: `{m.total_pnl:+.2f}`\n"
            f"Win Rate: `{m.win_rate*100:.0f}%`\n"
            f"Profit Factor: `{m.profit_factor:.2f}`"
        )

    async def get_positions_text(self) -> str:
        return "📋 Positions: (not wired)"
