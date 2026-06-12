"""
KAVACH-07 — Main Orchestrator
"""
from __future__ import annotations

import asyncio
import signal as os_signal
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

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

logger = get_logger("main")

class Kavach07:

    def __init__(self, config: Config):
        self._cfg = config
        self._db = Database(config)
        self._de = DataEngine(config)
        self._rm = RiskManager(config, self._db)
        self._se = SignalEngine(config, self._de)
        self._ee = ExecutionEngine(config, self._de, self._db, self._rm)
        self._ml = MLEngine(config)
        self._mon = MonitoringEngine(config, self._de, self._rm)
        self._tg = TelegramBot(config, self._db)
        self._shutdown = False
        self._scan_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        logger.info("🛡️ KAVACH-07 starting...")
        await self._db.connect()
        await self._rm.load()
        await self._rm.start_persistence()
        await self._de.start()
        await self._ee.start()

        self._se.on_signal(self._on_signal)
        self._ee.on_trade_closed(self._on_trade_closed)
        self._ee.on_position_opened(self._on_position_opened)
        self._mon.on_alert(self._tg.send)
        self._rm.register_halt_callback(self._tg.alert_circuit_breaker)

        self._tg.register_handlers(
            on_pause=self._rm.pause,
            on_resume=self._on_resume,
            on_halt=self._on_halt,
            status_provider=self._mon.get_status_text,
            balance_provider=self._mon.get_balance_text,
            positions_provider=self._mon.get_positions_text,
            report_provider=self._mon.get_status_text,
        )
        await self._tg.start()
        await self._mon.start()
        await self._ml.start()

        self._scan_task = asyncio.create_task(self._scan_loop(), name="signal_scan")
        logger.info("✅ KAVACH-07 fully operational")
        await self._tg.send("🛡️ *KAVACH-07 Online*\nAll systems nominal.")

    async def stop(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        logger.info("🛑 KAVACH-07 shutting down...")

        if self._scan_task:
            self._scan_task.cancel()
            await asyncio.gather(self._scan_task, return_exceptions=True)

        # FIX: Include MLEngine in shutdown
        components = [
            ("TelegramBot", self._tg),
            ("MonitoringEngine", self._mon),
            ("ExecutionEngine", self._ee),
            ("DataEngine", self._de),
            ("RiskManager", self._rm),
            ("MLEngine", self._ml),  # FIX: Was missing
        ]
        for name, component in components:
            try:
                await component.stop()
                logger.info(f"{name} stopped")
            except Exception as e:
                logger.error(f"Error stopping {name}: {e}")

        await self._db.close()
        logger.info("👋 KAVACH-07 shutdown complete")

    async def _scan_loop(self) -> None:
        while not self._shutdown:
            try:
                await self._se.scan_all()
            except Exception:
                logger.error(f"Scan error:\n{traceback.format_exc()}")
            await asyncio.sleep(self._cfg.SCAN_INTERVAL)

    async def _on_signal(self, sig) -> None:
        try:
            data = self._de.get_snapshot(sig.symbol)
            features = MLEngine.build_features(sig, data)
            ml_score = self._ml.predict(features)
            sig.ml_score = ml_score
            await self._tg.alert_signal(sig)
            self._mon.notify_signal()
            pos = await self._ee.execute_signal(sig)
            if pos:
                await self._tg.alert_trade_opened(pos)
        except Exception:
            logger.error(f"Signal processing error:\n{traceback.format_exc()}")

    async def _on_position_opened(self, pos) -> None:
        pass

    async def _on_trade_closed(self, result) -> None:
        try:
            await self._tg.alert_trade_closed(result)
            features = await self._get_signal_features(result)
            if features:
                self._ml.update(features, result)
        except Exception:
            logger.error(f"Trade close handler error:\n{traceback.format_exc()}")

    async def _get_signal_features(self, result) -> Optional[dict]:
        try:
            signals = await self._db.get_recent_signals(limit=100)
            for sig_row in signals:
                if (sig_row.get("symbol") == result.symbol and 
                    sig_row.get("strategy") == result.strategy and
                    sig_row.get("direction") == result.direction):
                    from models import Signal
                    sig = Signal(
                        symbol=sig_row["symbol"],
                        strategy=sig_row["strategy"],
                        direction=sig_row["direction"],
                        confidence=sig_row["confidence"],
                        entry_type=sig_row["entry_type"],
                        entry_price=sig_row["entry_price"],
                        sl_price=sig_row["sl_price"],
                        tp1_price=sig_row["tp1_price"],
                        tp2_price=sig_row.get("tp2_price"),
                        risk_pct=sig_row["risk_pct"],
                        rationale=sig_row.get("rationale", ""),
                        atr=sig_row.get("atr", 0),
                    )
                    data = self._de.get_snapshot(result.symbol)
                    return MLEngine.build_features(sig, data)
        except Exception:
            logger.warning(f"Could not retrieve signal features: {traceback.format_exc()}")
        return None

    async def _on_resume(self) -> None:
        self._rm.resume()
        if self._rm.metrics.circuit_state == "OK":
            await self._tg.send("▶️ Trading resumed")
        else:
            await self._tg.send(f"⚠️ Resume blocked: {self._rm.metrics.circuit_reason}")

    async def _on_halt(self) -> None:
        self._rm.halt()
        await self._ee.close_all_positions("EMERGENCY_HALT")
        await self._tg.send("🛑 Emergency halt triggered — all positions closed")

    def _handle_sigterm(self) -> None:
        logger.info("SIGTERM received")
        asyncio.create_task(self.stop())

async def main():
    config = Config()
    config._start_time = time.time()
    bot = Kavach07(config)
    loop = asyncio.get_running_loop()
    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        loop.add_signal_handler(sig, bot._handle_sigterm)
    try:
        await bot.start()
        while not bot._shutdown:
            await asyncio.sleep(1)
    finally:
        await bot.stop()

if __name__ == "__main__":
    asyncio.run(main())
