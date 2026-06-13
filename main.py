"""
KAVACH-07 — Main Entry Point
Initialises all components, runs the async event loop, handles graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

# Load .env before any other imports that might use env vars
load_dotenv()

from kavach.db.manager      import DBManager
from kavach.core.data_engine import DataEngine
from kavach.core.risk_manager import RiskManager
from kavach.core.meta_strategy import MetaStrategy
from kavach.core.alert_manager  import AlertManager
from kavach.ui.dashboard        import Dashboard


# ─────────────────────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(config: dict) -> None:
    lcfg     = config.get("logging", {})
    level    = getattr(logging, lcfg.get("level", "INFO").upper(), logging.INFO)
    fmt      = lcfg.get("format", "%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    log_file = lcfg.get("file", "logs/kavach.log")

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter(fmt))
    root.addHandler(ch)

    # Rotating file handler
    fh = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=int(lcfg.get("max_bytes", 10_485_760)),
        backupCount=int(lcfg.get("backup_count", 5)),
    )
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)

    # Silence noisy libraries
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


logger = logging.getLogger("kavach.main")


# ─────────────────────────────────────────────────────────────────────────────
# Config loader
# ─────────────────────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# KAVACH-07 Application
# ─────────────────────────────────────────────────────────────────────────────

class Kavach07:
    """Top-level application controller."""

    def __init__(self, config: dict) -> None:
        self._cfg  = config
        self._loop_interval = int(config.get("bot", {}).get("loop_interval_seconds", 30))
        self._status_interval = int(config.get("bot", {}).get("status_update_interval_seconds", 3600))

        # Initialise components (not yet started)
        db_path = os.getenv("DB_PATH", config.get("logging", {}).get("db_path", "kavach.db"))
        self._db        = DBManager(db_path)
        self._data_eng: Optional[DataEngine]   = None
        self._risk:     Optional[RiskManager]  = None
        self._meta:     Optional[MetaStrategy] = None
        self._alerts:   Optional[AlertManager] = None
        self._dash:     Optional[Dashboard]    = None

        self._running   = False
        self._last_status_ts: float = 0.0
        self._total_pnl: float = 0.0

    async def initialise(self) -> None:
        """Initialise all components in dependency order."""
        logger.info("═" * 60)
        logger.info("KAVACH-07 initialising…")

        # Database
        await self._db.initialize()
        logger.info("✓ DB ready")

        # Data Engine
        self._data_eng = DataEngine(self._cfg, self._db)

        # Risk Manager
        self._risk = RiskManager(self._cfg, self._db)

        # Meta-Strategy
        self._meta = MetaStrategy(self._cfg, self._data_eng.symbols)

        # Alert Manager
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id   = os.getenv("TELEGRAM_CHAT_ID",   "")
        if not bot_token or not chat_id:
            logger.critical("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env!")
            raise EnvironmentError("Telegram credentials missing — see .env.example")

        self._alerts = AlertManager(
            self._cfg, self._db, self._risk, bot_token, chat_id
        )
        self._alerts.set_confirm_callback(self._on_trade_confirmed)

        # Dashboard
        self._dash = Dashboard(self._cfg)

        logger.info("✓ All components initialised")

    async def run(self) -> None:
        """Start all components and enter the main signal loop."""
        self._running = True

        # Start background services
        await self._alerts.start()
        await self._data_eng.start()

        # Start dashboard (non-blocking)
        try:
            self._dash.start()
        except Exception as exc:
            logger.warning("Dashboard start failed (non-critical): %s", exc)

        logger.info("KAVACH-07 RUNNING — symbols: %s", self._data_eng.symbols)
        await self._alerts.send_text(
            "🛡️ *KAVACH\\-07 fully operational\\.*\n"
            f"Tracking `{len(self._data_eng.symbols)}` symbols\\."
        )

        try:
            await self._main_loop()
        except asyncio.CancelledError:
            logger.info("Main loop cancelled — shutting down…")
        finally:
            await self.shutdown()

    async def _main_loop(self) -> None:
        """Core signal generation → risk filtering → alert cycle."""
        while self._running:
            tick_start = time.monotonic()
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Main loop tick error: %s", exc, exc_info=True)
                try:
                    await self._alerts.send_error_alert("main_loop", str(exc))
                except Exception:
                    pass

            # Periodic status update
            now = time.time()
            if now - self._last_status_ts >= self._status_interval:
                await self._send_status_update()
                self._last_status_ts = now

            # Sleep remaining time in loop interval
            elapsed = time.monotonic() - tick_start
            sleep_for = max(0.0, self._loop_interval - elapsed)
            logger.debug("Tick completed in %.2fs. Sleeping %.2fs.", elapsed, sleep_for)
            await asyncio.sleep(sleep_for)

    async def _tick(self) -> None:
        """One signal generation cycle."""
        if self._risk.is_paused:
            logger.debug("Bot paused — skipping tick.")
            self._update_dashboard(signals=[], paused=True)
            return

        # 1. Get current market data context
        data_ctx = self._data_eng.get_data_context()

        # 2. Generate raw MetaSignals from all strategies
        raw_signals = await self._meta.analyze(data_ctx)

        approved_signals = []
        for raw_sig in raw_signals:
            # 3. Apply risk filters
            sig = await self._risk.filter_signal(raw_sig, data_ctx)
            if sig is None:
                continue

            # 4. Persist signal to DB
            try:
                signal_id = await self._db.insert_signal(sig)
            except Exception as exc:
                logger.error("DB insert_signal failed: %s", exc)
                continue

            # 5. Send Telegram alert
            try:
                await self._alerts.send_signal_alert(sig, signal_id)
                approved_signals.append(sig)
                logger.info(
                    "SIGNAL → %s %s conf=%.1f entry=%.6g SL=%.6g TP=%.6g",
                    sig.symbol, sig.side, sig.confidence,
                    sig.entry, sig.stop_loss, sig.take_profit,
                )
            except Exception as exc:
                logger.error("send_signal_alert failed: %s", exc)

        # 6. Update dashboard
        await self._update_dashboard_from_db(approved_signals)

    async def _update_dashboard_from_db(self, new_signals: list) -> None:
        """Refresh dashboard with latest DB data."""
        try:
            strat_perf   = await self._db.get_all_strategy_performance()
            open_trades  = await self._db.get_open_trades()
            risk_status  = self._risk.status_dict()
            daily_pnl    = risk_status.get("daily_pnl", 0.0)

            # Determine dominant regime (most common across symbols)
            data_ctx    = self._data_eng.get_data_context()
            regime      = _dominant_regime(data_ctx)

            sig_dicts = [
                {
                    "symbol":         s.symbol,
                    "side":           s.side,
                    "confidence":     s.confidence,
                    "entry":          s.entry,
                    "stop_loss":      s.stop_loss,
                    "take_profit":    s.take_profit,
                    "regime":         s.regime,
                    "strategies_fired": s.strategies_fired,
                }
                for s in new_signals
            ]

            if self._dash:
                self._dash.update(
                    signals=sig_dicts,
                    strat_perf=strat_perf,
                    open_trades=open_trades,
                    daily_pnl=daily_pnl,
                    total_pnl=self._total_pnl,
                    api_health=self._data_eng.api_health,
                    regime=regime,
                    bot_status="PAUSED" if self._risk.is_paused else "RUNNING",
                    market_data={s: self._data_eng.get_data_context().get(s)
                                 for s in self._data_eng.symbols},
                )
        except Exception as exc:
            logger.debug("Dashboard update error: %s", exc)

    def _update_dashboard(self, signals: list, paused: bool = False) -> None:
        if self._dash:
            self._dash.update(
                signals=signals,
                bot_status="PAUSED" if paused else "RUNNING",
                api_health=self._data_eng.api_health if self._data_eng else {},
            )

    async def _send_status_update(self) -> None:
        try:
            open_trades = await self._db.get_open_trades()
            risk_status = self._risk.status_dict()
            await self._alerts.send_status_update(risk_status, open_trades)
        except Exception as exc:
            logger.warning("_send_status_update error: %s", exc)

    async def _on_trade_confirmed(
        self, signal_id: int, trade_id: int, signal: Any
    ) -> None:
        """Called when user confirms a signal via Telegram YES button."""
        logger.info(
            "Trade confirmed: signal_id=%d trade_id=%d %s %s @ %.6g",
            signal_id, trade_id, signal.symbol, signal.side, signal.entry,
        )
        await self._db.log_event(
            "INFO", "trade_confirm",
            f"Trade {trade_id} confirmed for signal {signal_id} "
            f"{signal.symbol} {signal.side} @ {signal.entry:.6g}",
        )

    async def shutdown(self) -> None:
        """Graceful shutdown of all components."""
        logger.info("KAVACH-07 shutting down…")
        self._running = False
        try:
            if self._dash:
                self._dash.stop()
        except Exception:
            pass
        try:
            if self._data_eng:
                await self._data_eng.stop()
        except Exception as exc:
            logger.error("DataEngine stop error: %s", exc)
        try:
            if self._alerts:
                await self._alerts.stop()
        except Exception as exc:
            logger.error("AlertManager stop error: %s", exc)
        try:
            await self._db.close()
        except Exception as exc:
            logger.error("DB close error: %s", exc)
        logger.info("KAVACH-07 shutdown complete.")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _dominant_regime(data_ctx: Dict[str, Any]) -> str:
    """Find the most common regime string across all MarketData objects."""
    from collections import Counter
    regimes = []
    for key, val in data_ctx.items():
        if hasattr(val, "adx"):     # It's a MarketData object
            # Regime is embedded in data_ctx per the RegimeFilter metadata flow
            pass
    regime_counter = data_ctx.get("_regime_counter")
    if regime_counter and isinstance(regime_counter, dict):
        return max(regime_counter, key=regime_counter.get, default="UNDEFINED")
    return "UNDEFINED"


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def _async_main() -> None:
    config = load_config("config.yaml")
    setup_logging(config)
    app = Kavach07(config)

    # Handle SIGTERM / SIGINT gracefully
    loop = asyncio.get_running_loop()

    def _handle_signal(sig_num: int) -> None:
        logger.info("Signal %d received — requesting shutdown.", sig_num)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            pass  # Windows

    try:
        await app.initialise()
        await app.run()
    except asyncio.CancelledError:
        logger.info("Application cancelled.")
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        raise
    finally:
        try:
            await app.shutdown()
        except Exception:
            pass


def main() -> None:
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        logger.info("Interrupted by keyboard.")
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
