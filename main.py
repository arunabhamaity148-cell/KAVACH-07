"""
KAVACH-07 — Main Orchestrator
Wires all components together. Manages the main scan loop,
graceful startup/shutdown, and SIGINT/SIGTERM handling.
"""
from __future__ import annotations

import asyncio
import signal
import sys
import time
import traceback

from config import Config, get_config
from data_engine import DataEngine
from database import Database
from execution_engine import ExecutionEngine
from ml_engine import MLEngine
from models import Position, Signal, TradeResult
from monitoring import MonitoringEngine
from risk_manager import RiskManager
from signal_engine import SignalEngine
from telegram_bot import TelegramBot
from utils import setup_logging, get_logger

logger = get_logger(__name__)

_BANNER = r"""
 _  __    ___  _   _   ___  _  _       ___   ____
| |/ /   / _ \| | | | / _ \| || |     / _ \ |___  |
| ' /   | |_| | | | || |_| | || |_   | | | |   / /
| . \   |  _  | |_| ||  _  |__   _|  | |_| |  / /
|_|\_\  |_| |_|\___/ |_| |_|  |_|     \___/  /_/

KAVACH-07 | Futures Signal Bot | SIGNALS ONLY — NO AUTO-TRADE
"""


class KAVACH07:
    """
    Top-level orchestrator.
    Startup order:
      1. Config + logging
      2. Database
      3. ML Engine (load model)
      4. Risk Manager (load state)
      5. Data Engine (bootstrap + WS)
      6. Signal Engine
      7. Execution Engine (paper)
      8. Monitoring Engine
      9. Telegram Bot
     10. Main scan loop
    """

    def __init__(self):
        self._cfg: Config = get_config()
        self._shutdown_event = asyncio.Event()

        # Components (set during start)
        self._db: Database | None = None
        self._ml: MLEngine | None = None
        self._rm: RiskManager | None = None
        self._de: DataEngine | None = None
        self._se: SignalEngine | None = None
        self._ee: ExecutionEngine | None = None
        self._mon: MonitoringEngine | None = None
        self._tg: TelegramBot | None = None

    # ─── Startup ─────────────────────────────────────────────

    async def start(self) -> None:
        cfg = self._cfg
        print(_BANNER)
        logger.info(cfg.summary())

        # 1. Database
        self._db = Database()
        await self._db.connect()

        # 2. ML Engine
        self._ml = MLEngine(min_samples=cfg.ML_MIN_SAMPLES)

        # 3. Risk Manager
        self._rm = RiskManager(cfg, self._db)
        await self._rm.load()
        await self._rm.start_persistence()

        # 4. Data Engine
        self._de = DataEngine(cfg, self._db)
        await self._de.start()

        logger.info("Waiting for initial data...")
        try:
            await self._de.wait_ready()
            logger.info("Data engine ready")
        except asyncio.TimeoutError:
            logger.warning("Data engine timeout — continuing anyway")

        # 5. Signal Engine
        self._se = SignalEngine(cfg, self._de, self._db, self._ml)

        # 6. Execution Engine
        self._ee = ExecutionEngine(cfg, self._de, self._db, self._rm)
        await self._ee.start()

        # 7. Monitoring Engine
        self._mon = MonitoringEngine(cfg, self._de, self._db, self._rm, self._ml)
        await self._mon.start()

        # 8. Telegram Bot
        self._tg = TelegramBot(cfg, self._db)
        self._wire_telegram()
        await self._tg.start()

        # 9. Wire callbacks
        self._wire_callbacks()

        logger.info("All components started — entering main scan loop")
        await self._scan_loop()

    # ─── Main scan loop ──────────────────────────────────────

    async def _scan_loop(self) -> None:
        cfg = self._cfg
        last_regime_alert: str = ""

        while not self._shutdown_event.is_set():
            try:
                t0 = time.monotonic()

                # Check if risk manager is halted
                if self._rm.is_halted:
                    logger.debug("System halted — skipping scan")
                    await asyncio.sleep(cfg.SCAN_INTERVAL)
                    continue

                # Run scan
                signals = await self._se.run_scan()

                # Process each approved signal
                for sig in signals:
                    try:
                        await self._process_signal(sig)
                    except Exception:
                        logger.error(f"Signal process error:\n{traceback.format_exc()}")

                # Regime change alert (debounced)
                regime = self._de.get_regime()
                if regime:
                    regime_key = f"{regime.bias}:{regime.confidence:.2f}"
                    if regime_key != last_regime_alert:
                        last_regime_alert = regime_key
                        asyncio.create_task(self._tg.alert_regime(regime))

                # Track metrics
                self._rm.increment_signals()

                # Sleep until next scan (accounting for scan duration)
                elapsed = time.monotonic() - t0
                sleep_time = max(1.0, cfg.SCAN_INTERVAL - elapsed)
                await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.error(f"Scan loop error:\n{traceback.format_exc()}")
                self._mon.notify_error(
                    "scan_loop",
                    "Scan loop error",
                    traceback.format_exc(),
                )
                await asyncio.sleep(10)

        logger.info("Scan loop exited")

    async def _process_signal(self, sig: Signal) -> None:
        """Handle a single approved signal: notify + paper trade."""
        # Telegram alert
        await self._tg.alert_signal(sig)
        self._mon.notify_signal()

        # Paper trade execution
        pos = await self._ee.execute_signal(sig)

        if pos:
            await self._tg.alert_trade_opened(pos)
            logger.info(
                f"Signal → Trade: {sig.symbol} {sig.direction} "
                f"@ {pos.entry_price:.6g} | Strategy: {sig.strategy}"
            )
        else:
            logger.info(
                f"Signal fired (no trade): {sig.symbol} {sig.direction} "
                f"@ {sig.entry_price:.6g} | Risk rejected or order missed"
            )

    # ─── Wiring ──────────────────────────────────────────────

    def _wire_callbacks(self) -> None:
        """Wire execution → ML update and trade alert."""

        async def on_trade_closed(result: TradeResult) -> None:
            # Update ML with outcome
            # We'd need the original features here — store them or rebuild from DB
            # For simplicity, we do a lightweight rebuild
            features = self._build_features_for_result(result)
            win = result.pnl > 0
            self._ml.update(features, win)

            # Telegram alert
            await self._tg.alert_trade_closed(result)

        async def on_position_opened(pos: Position) -> None:
            pass  # Already alerted in _process_signal

        self._ee.on_trade_closed(on_trade_closed)
        self._ee.on_position_opened(on_position_opened)
        self._mon.on_alert(self._tg.send)

    def _build_features_for_result(self, result: TradeResult) -> dict:
        """Rebuild approximate ML features from a trade result."""
        snap = self._de.get_snapshot(result.symbol)
        if snap:
            from signal_engine import SignalEngine
            # Use a minimal Signal for feature building
            from models import Signal as Sig
            dummy = Sig(
                symbol=result.symbol, strategy=result.strategy,
                direction=result.direction, confidence=result.confidence,
                entry_type="MARKET", entry_price=result.entry_price,
                sl_price=result.entry_price * (0.99 if result.direction == "LONG" else 1.01),
                tp1_price=result.entry_price * (1.01 if result.direction == "LONG" else 0.99),
                atr=snap.atr_5m,
            )
            return self._se._build_features(snap, dummy)
        # Fallback: empty features
        return {}

    def _wire_telegram(self) -> None:
        """Register all Telegram command providers and callbacks."""
        se, rm, ee, mon = self._se, self._rm, self._ee, self._mon

        async def on_pause():
            se.pause()
            rm.pause()
            logger.info("Paused by Telegram command")

        async def on_resume():
            se.resume()
            rm.resume()
            logger.info("Resumed by Telegram command")

        async def on_halt():
            rm.halt()
            await self._ee.close_all_positions("MANUAL_HALT")
            logger.warning("Emergency halt by Telegram command")

        async def status_provider():
            return await mon.build_status_message()

        async def balance_provider():
            m = rm.metrics
            return (
                f"💰 *Balance*\n"
                f"Balance: `${m.balance:.2f}`\n"
                f"Peak: `${m.peak_balance:.2f}`\n"
                f"Total PnL: `{m.total_pnl:+.2f}`\n"
                f"Daily PnL: `{m.daily_pnl:+.2f}`\n"
                f"Drawdown: `{m.drawdown*100:.1f}%`\n"
                f"Win Rate: `{m.win_rate*100:.0f}%` ({m.total_trades} trades)\n"
                f"Profit Factor: `{m.profit_factor:.2f}`"
            )

        async def positions_provider():
            open_pos = ee.get_open_positions()
            if not open_pos:
                return "📋 No open positions"
            lines = ["📋 *Open Positions*\n"]
            for pos in open_pos:
                current = self._de.get_current_price(pos.symbol)
                upnl = pos.calc_unrealized_pnl(current) if current > 0 else 0.0
                upnl_icon = "✅" if upnl >= 0 else "🔴"
                lines.append(
                    f"{upnl_icon} {pos.symbol} {pos.direction}\n"
                    f"  Entry: `{pos.entry_price:.6g}` | Now: `{current:.6g}`\n"
                    f"  PnL: `{upnl:+.4f}` | {pos.strategy}"
                )
            return "\n".join(lines)

        async def report_provider():
            return await mon.build_hourly_report()

        self._tg.register_handlers(
            on_pause=on_pause,
            on_resume=on_resume,
            on_halt=on_halt,
            status_provider=status_provider,
            balance_provider=balance_provider,
            positions_provider=positions_provider,
            report_provider=report_provider,
        )

    # ─── Shutdown ─────────────────────────────────────────────

    async def shutdown(self) -> None:
        logger.info("Shutdown initiated...")
        self._shutdown_event.set()

        # Stop components in reverse order
        components = [
            ("TelegramBot",       self._tg),
            ("MonitoringEngine",  self._mon),
            ("ExecutionEngine",   self._ee),
            ("DataEngine",        self._de),
            ("RiskManager",       self._rm),
        ]
        for name, comp in components:
            if comp:
                try:
                    await comp.stop()
                    logger.info(f"{name} stopped")
                except Exception as e:
                    logger.error(f"{name} stop error: {e}")

        if self._db:
            await self._db.close()
            logger.info("Database closed")

        logger.info("KAVACH-07 shutdown complete")


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

def main() -> None:
    # Configure logging before anything else
    cfg = get_config()
    setup_logging(cfg.LOG_LEVEL, cfg.LOG_FILE)

    app = KAVACH07()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _sig_handler(sig_name: str) -> None:
        logger.info(f"Received {sig_name} — initiating graceful shutdown")
        loop.create_task(app.shutdown())

    # Register OS signals
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig.name: _sig_handler(s))

    try:
        loop.run_until_complete(app.start())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received")
        loop.run_until_complete(app.shutdown())
    except Exception:
        logger.critical(f"Fatal error:\n{traceback.format_exc()}")
        loop.run_until_complete(app.shutdown())
        sys.exit(1)
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            loop.close()
            logger.info("Event loop closed")


if __name__ == "__main__":
    main()
