"""
KAVACH-07 — Main Orchestrator
Production-grade asyncio loop coordinating data, strategy, risk, and execution.
Enforces warmup periods, staleness watchdogs, and graceful shutdowns.
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

# Internal Imports
from kavach.core.data_engine import DataEngine
from kavach.core.risk_manager import RiskManager
from kavach.core.meta_strategy import MetaStrategy
from kavach.core.alert_manager import AlertManager
from kavach.core.news_engine import NewsEngine
from kavach.core.whale_engine import WhaleEngine
from kavach.core.exchange_connector import HyperliquidConnector
from kavach.db.manager import DBManager
from kavach.ui.dashboard import Dashboard
from kavach.strategies.liquidity_flow import OrderBookManager

# Load environment variables
load_dotenv()

def setup_logging(config: dict) -> None:
    """Configures centralized logging with rotation."""
    log_cfg = config.get("logging", {})
    log_file = log_cfg.get("file", "logs/kavach.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    fmt = log_cfg.get("format", "%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=log_cfg.get("max_bytes", 10485760),
            backupCount=log_cfg.get("backup_count", 5)
        )
    ]

    logging.basicConfig(level=level, format=fmt, handlers=handlers)
    # Silence noisy libraries
    for lib in ["websockets", "aiohttp", "telegram", "urllib3"]:
        logging.getLogger(lib).setLevel(logging.WARNING)

logger = logging.getLogger("kavach.main")

class KavachBot:
    """
    Main application class for KAVACH-07.
    Zero-silent-failure design with defensive resource management.
    """

    def __init__(self, config: dict):
        self._cfg = config
        self._loop_interval = int(config["bot"]["loop_interval_seconds"])
        self._warmup_duration = int(config["bot"]["warmup_duration_seconds"])
        self._boot_time = time.time()
        self._running = False
        
        # Core Components
        self._db = DBManager(os.getenv("DB_PATH", "kavach.db"))
        self._data_engine = DataEngine(config)
        self._risk_manager = RiskManager(config, self._db)
        self._ob_manager = OrderBookManager(self._data_engine._symbols, config)
        self._meta_strategy = MetaStrategy(config, self._data_engine._symbols, self._ob_manager)
        
        self._news_engine = NewsEngine(
            config, self._risk_manager, os.getenv("OPENAI_API_KEY", "")
        )
        self._whale_engine = WhaleEngine(
            config, os.getenv("WHALE_ALERT_API_KEY", "")
        )
        self._exchange = HyperliquidConnector(config)
        
        self._alerts = AlertManager(
            config, self._db, self._risk_manager,
            os.getenv("TELEGRAM_BOT_TOKEN", ""),
            os.getenv("TELEGRAM_CHAT_ID", "")
        )
        
        # UI
        self._dashboard = Dashboard(config)
        
        # Wiring
        self._risk_manager.set_engines(self._news_engine, self._whale_engine)
        self._meta_strategy.set_news_engine(self._news_engine)
        self._meta_strategy.set_whale_engine(self._whale_engine)
        self._news_engine.set_alert_manager(self._alerts)
        self._alerts.set_confirm_callback(self._on_trade_confirmed)

    async def initialise(self) -> None:
        """Sequential async startup of all modules."""
        logger.info("🛡️ KAVACH-07 v7.0.0 remediated build initialising...")
        
        await self._db.initialize()
        await self._risk_manager.recover_state()
        await self._exchange.connect()
        
        logger.info("✓ Core services ready.")

    async def run(self) -> None:
        """Execution entry point."""
        self._running = True
        
        # Start background tasks
        await self._alerts.start()
        await self._data_engine.start()
        await self._ob_manager.start()
        await self._news_engine.start()
        await self._whale_engine.start()

        # Enter UI loop and Main loop concurrently
        try:
            with self._dashboard.start():
                await self._main_loop()
        except asyncio.CancelledError:
            logger.info("Main loop task cancelled.")
        except Exception as e:
            logger.critical("Fatal application error: %s", e, exc_info=True)
        finally:
            await self.shutdown()

    async def _main_loop(self) -> None:
        """Primary trading tick orchestrator."""
        while self._running:
            tick_start = time.monotonic()
            
            try:
                # 1. Warmup Check (Bug #29)
                if not self._is_warmed_up():
                    self._dashboard.update_state(bot_status="WARMING UP")
                else:
                    # 2. Staleness Watchdog (Bug #27)
                    if not self._data_engine.is_healthy():
                        logger.critical("WATCHDOG: Stale data detected. Skipping cycle.")
                        self._dashboard.update_state(bot_status="STALE DATA")
                    else:
                        # 3. Standard Tick
                        await self._tick()
                
                # 4. Refresh Dashboard
                await self._refresh_ui()

            except Exception as e:
                logger.error("Error in main tick: %s", e, exc_info=True)
                await self._alerts.send_text(f"🚨 *SYSTEM ERROR:* `{str(e)[:100]}`")

            # 5. Precise timing control
            elapsed = time.monotonic() - tick_start
            sleep_time = max(0.1, self._loop_interval - elapsed)
            await asyncio.sleep(sleep_time)

    async def _tick(self) -> None:
        """Cycle logic: Analyze -> Filter -> Notify -> Persist."""
        if self._risk_manager.is_paused:
            self._dashboard.update_state(bot_status="PAUSED")
            return

        self._dashboard.update_state(bot_status="RUNNING")
        
        # Data Context for strategies
        data_ctx = {
            s: self._data_engine.get_market_data(s) for s in self._data_engine._symbols
        }
        # Add external metrics
        news_status = self._news_engine.get_status()
        data_ctx["news_score"] = news_status["score"]
        data_ctx["fear_and_greed_index"] = None # Placeholder for Engine integration
        
        # Analysis
        raw_signals = await self._meta_strategy.analyze(data_ctx)
        
        # Risk Filtration
        for raw_sig in raw_signals:
            try:
                # Isolate per-symbol signal processing
                final_sig = await self._risk_manager.filter_signal(raw_sig, data_ctx)
                if final_sig:
                    # Persist Signal to DB
                    signal_id = await self._db.insert_signal(final_sig)
                    # Notify user for confirmation
                    await self._alerts.send_signal_alert(final_sig, signal_id)
                    logger.info("Signal Generated: %s %s (Conf: %.1f%%)", 
                                final_sig.symbol, final_sig.side, final_sig.confidence)
            except Exception as e:
                logger.error("Signal processing failed for %s: %s", raw_sig.symbol, e)

    async def _on_trade_confirmed(self, signal_id: int, signal: Any) -> None:
        """Callback invoked when user clicks YES on Telegram."""
        try:
            # 1. Execute on Exchange
            success = await self._exchange.execute_signal(signal)
            
            if success:
                # 2. Record Trade in DB
                trade_id = await self._db.insert_trade(
                    signal_id=signal_id,
                    symbol=signal.symbol,
                    side=signal.side,
                    entry=signal.entry,
                    size=signal.position_size_usdt
                )
                logger.info("Trade EXECUTED: %s %s ID: %d", signal.symbol, signal.side, trade_id)
                await self._alerts.send_text(f"✅ *EXECUTED:* `{signal.symbol}` Trade ID: `{trade_id}`")
            else:
                await self._alerts.send_text(f"⚠️ *EXECUTION FAILED:* Exchange rejected order for `{signal.symbol}`")

        except Exception as e:
            logger.error("Trade confirmation failure: %s", e)
            await self._alerts.send_text(f"❌ *EXECUTION ERROR:* `{str(e)}`")

    async def _refresh_ui(self) -> None:
        """Gathers latest metrics for the dashboard."""
        daily_pnl = await self._db.get_daily_pnl()
        total_pnl = await self._db.get_total_pnl()
        open_trades = await self._db.get_open_trades()
        latest_signals = await self._db._fetchall("SELECT * FROM signals ORDER BY timestamp DESC LIMIT 5")
        
        # Extract regime from a major symbol (BTC)
        regime = "UNDEFINED"
        btc_md = self._data_engine.get_market_data("BTCUSDT")
        if btc_md:
            # We determine regime in the MetaStrategy, dashboard just needs the label
            # This logic can be more robust by tracking global regime state
            pass 

        self._dashboard.update_state(
            daily_pnl=daily_pnl,
            total_pnl=total_pnl,
            open_trades_count=len(open_trades),
            signals=latest_signals,
            api_status={
                "Binance": self._data_engine.is_healthy(),
                "HL": self._exchange._session is not None,
                "News": not self._news_engine._is_degraded
            }
        )

    def _is_warmed_up(self) -> bool:
        """Check if bot has sufficient data and time to trade."""
        if time.time() - self._boot_time < self._warmup_duration:
            return False
        
        for md in self._data_engine._data.values():
            if not md.is_warm:
                return False
        return True

    async def shutdown(self) -> None:
        """Orchestrated cleanup."""
        logger.info("KAVACH-07 shutting down...")
        self._running = False
        
        # Stop engines first
        await self._alerts.stop()
        await self._news_engine.stop()
        await self._whale_engine.stop()
        await self._data_engine.stop()
        await self._ob_manager.stop()
        await self._exchange.disconnect()
        await self._db.close()
        
        logger.info("Shutdown complete. 🛡️")

def main() -> None:
    """Application entry point with system signal handling."""
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    setup_logging(config)
    
    bot = KavachBot(config)
    
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("Interrupt received. Stopping bot...")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, signal_handler)

    try:
        loop.run_until_complete(bot.initialise())
        loop.run_until_complete(bot.run())
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.critical("Fatal exit: %s", e)
        sys.exit(1)
    finally:
        loop.close()

if __name__ == "__main__":
    main()