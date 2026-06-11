"""
KAVACH-07 — Main Orchestrator
Startup, graceful shutdown, and main scan loop.
"""
from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime, timezone

from config import Config
from data_engine import DataEngine
from database import Database
from execution_engine import ExecutionEngine
from ml_engine import MLEngine
from monitoring import MonitoringEngine
from risk_manager import RiskManager
from signal_engine import SignalEngine
from telegram_bot import TelegramBot
from utils import get_logger

logger = get_logger(__name__)


class KAVACH07:
    def __init__(self):
        self._cfg = Config.from_env()
        self._db = Database(self._cfg)
        self._risk = RiskManager(self._cfg)
        self._data = DataEngine(self._cfg, self._db)
        self._ml = MLEngine(self._cfg)
        self._exec = ExecutionEngine(self._cfg, self._db, self._risk)
        self._monitor = MonitoringEngine(self._cfg, self._risk)
        self._signal = SignalEngine(self._cfg, self._data, self._ml)
        self._telegram = TelegramBot(self._cfg, self._db)

        self._running = False
        self._paused = False

    # ─── Lifecycle ───────────────────────────────────────────

    async def start(self) -> None:
        logger.info(
            f"KAVACH-07 | Mode={'LIVE' if not self._cfg.USE_TESTNET else 'TESTNET'} | "
            f"Balance=${self._risk.balance:.0f} | Pairs={len(self._cfg.BASE_PAIRS)} | "
            f"Strategies={len(self._cfg.STRATEGIES)} | Risk={self._cfg.MAX_RISK_PER_TRADE*100:.1f}%/trade"
        )

        await self._db.connect()
        await self._data.start()
        await self._exec.start()
        await self._monitor.start()
        await self._telegram.start()

        # Wire Telegram callbacks
        self._telegram.register_handlers(
            on_pause=self._pause,
            on_resume=self._resume,
            on_halt=self._halt,
            status_provider=self._monitor.status_text,
            balance_provider=self._monitor.balance_text,
            signals_provider=None,
            trades_provider=None,
            positions_provider=self._exec.positions_text,
            report_provider=self._monitor.hourly_report,
        )

        self._running = True
        logger.info("All components started — entering main scan loop")

        # Graceful shutdown handlers
        loop = asyncio.get_event_loop()
        for sig_name in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig_name, lambda: asyncio.create_task(self.stop()))

        try:
            await self._scan_loop()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        logger.info("Shutdown initiated...")

        await self._telegram.stop()
        await self._monitor.stop()
        await self._exec.stop()
        await self._data.stop()
        self._risk._save_state()
        await self._db.close()

        logger.info("KAVACH-07 shutdown complete")

    # ─── Scan loop ───────────────────────────────────────────

    async def _scan_loop(self) -> None:
        while self._running:
            try:
                self._risk.reset_daily()

                if self._paused:
                    await asyncio.sleep(self._cfg.SCAN_INTERVAL)
                    continue

                state, reason = self._risk.check_circuit_breakers()
                if state == "HALT":
                    self._risk.set_circuit_state("HALT", reason)
                    await self._telegram.alert_circuit_breaker("HALT", reason)
                    self._paused = True
                    await asyncio.sleep(self._cfg.SCAN_INTERVAL)
                    continue

                # Run scan
                signals = await self._signal.run_scan()

                # FIX: Only increment for valid signals (not scans)
                if signals:
                    self._risk.increment_signals(len(signals))

                    for sig in signals:
                        await self._process_signal(sig)

                await self._monitor.record_scan()

                # Hourly report
                if self._monitor.should_report():
                    report = self._monitor.hourly_report()
                    await self._telegram.send(report)

                await asyncio.sleep(self._cfg.SCAN_INTERVAL)

            except Exception as e:
                logger.error(f"Scan loop error: {e}")
                await asyncio.sleep(5)

    async def _process_signal(self, sig) -> None:
        if sig.confidence < self._cfg.MIN_SIGNAL_CONFIDENCE:
            return

        if sig.ml_score < self._cfg.ML_CONFIDENCE_THRESHOLD:
            return

        await self._telegram.alert_signal(sig)

        # Paper trade only
        trade = await self._exec.open_paper_trade(sig)
        if trade:
            await self._telegram.alert_trade_opened(trade)

    # ─── Telegram callbacks ──────────────────────────────────

    async def _pause(self) -> None:
        self._paused = True
        logger.info("Signal generation paused")

    async def _resume(self) -> None:
        self._paused = False
        self._risk.set_circuit_state("OK", "")
        logger.info("Signal generation resumed")

    async def _halt(self) -> None:
        self._paused = True
        self._risk.set_circuit_state("HALT", "Manual halt")
        logger.info("Emergency halt activated")


# ─── Entry point ───────────────────────────────────────────

if __name__ == "__main__":
    bot = KAVACH07()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        asyncio.run(bot.stop())
