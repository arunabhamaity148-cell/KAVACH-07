"""
KAVACH-07 — Telegram Bot
Alerts, commands, and status queries.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable, List, Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from config import Config
from database import Database
from models import Position, RegimeSignal, Signal, TradeResult
from utils import get_logger

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────

class TelegramBot:

    def __init__(self, config: Config, db: Database):
        self._cfg = config
        self._db = db
        self._app: Optional[Application] = None
        self._handlers_registered = False

        # Callbacks wired by main.py
        self._on_pause: Optional[Callable[[], Awaitable[None]]] = None
        self._on_resume: Optional[Callable[[], Awaitable[None]]] = None
        self._on_halt: Optional[Callable[[], Awaitable[None]]] = None
        self._status_provider: Optional[Callable[[], Awaitable[str]]] = None
        self._balance_provider: Optional[Callable[[], Awaitable[str]]] = None
        self._positions_provider: Optional[Callable[[], Awaitable[str]]] = None
        self._report_provider: Optional[Callable[[], Awaitable[str]]] = None

    # ─── Lifecycle ───────────────────────────────────────────

    async def start(self) -> None:
        self._app = Application.builder().token(self._cfg.TELEGRAM_BOT_TOKEN).build()

        # Register commands
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_start))  # FIX: Add /help
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("balance", self._cmd_balance))
        self._app.add_handler(CommandHandler("signals", self._cmd_signals))
        self._app.add_handler(CommandHandler("trades", self._cmd_trades))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("config", self._cmd_config))
        self._app.add_handler(CommandHandler("report", self._cmd_report))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        self._app.add_handler(CommandHandler("halt", self._cmd_halt))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

        logger.info("Telegram bot started")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram bot stopped")

    # ─── Handler wiring ──────────────────────────────────────

    def register_handlers(
        self,
        on_pause: Callable[[], Awaitable[None]],
        on_resume: Callable[[], Awaitable[None]],
        on_halt: Callable[[], Awaitable[None]],
        status_provider: Callable[[], Awaitable[str]],
        balance_provider: Callable[[], Awaitable[str]],
        positions_provider: Callable[[], Awaitable[str]],
        report_provider: Callable[[], Awaitable[str]],
    ) -> None:
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_halt = on_halt
        self._status_provider = status_provider
        self._balance_provider = balance_provider
        self._positions_provider = positions_provider
        self._report_provider = report_provider
        self._handlers_registered = True

    # ─── Commands ────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "🛡️ *KAVACH-07 Bot*\n\n"
            "/status — Bot health\n"
            "/balance — P&L, drawdown, win rate\n"
            "/signals — Last 5 signals\n"
            "/trades — Last 5 closed trades\n"
            "/positions — Open positions\n"
            "/config — Current settings\n"
            "/report — Hourly report\n"
            "/pause — Pause signals\n"
            "/resume — Resume signals\n"
            "/halt — Emergency stop",
            parse_mode="Markdown",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._status_provider:
            try:
                msg = await self._status_provider()
            except Exception as e:
                logger.error(f"Status provider error: {e}")
                msg = "⚠️ Error fetching status"
        else:
            msg = "Status not available"
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._balance_provider:
            try:
                msg = await self._balance_provider()
            except Exception as e:
                logger.error(f"Balance provider error: {e}")
                msg = "⚠️ Error fetching balance"
        else:
            msg = "Balance not available"
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _cmd_signals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            rows = await self._db.get_recent_signals(limit=5)
        except Exception as e:
            logger.error(f"Signals DB error: {e}")
            await update.message.reply_text("⚠️ Error fetching signals")
            return
            
        if not rows:
            await update.message.reply_text("📡 No recent signals")
            return

        lines = ["📡 *Last 5 Signals*\n"]
        for r in rows:
            ts = self._get_ist_time(r.get("timestamp"))
            lines.append(
                f"{r['symbol']} {r['direction']} | {r['strategy']}\n"
                f"Conf: {r['confidence']:.0%} | {ts}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            rows = await self._db.get_recent_trades(limit=5)
        except Exception as e:
            logger.error(f"Trades DB error: {e}")
            await update.message.reply_text("⚠️ Error fetching trades")
            return
            
        if not rows:
            await update.message.reply_text("📋 No closed trades")
            return

        lines = ["📋 *Last 5 Trades*\n"]
        for r in rows:
            icon = "✅" if r["pnl"] > 0 else "🔴"
            ts = self._get_ist_time(r.get("close_time"))
            lines.append(
                f"{icon} {r['symbol']} {r['direction']} | {r['strategy']}\n"
                f"PnL: `{r['pnl']:+.4f}` | R: `{r['r_multiple']:+.2f}R` | {ts}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._positions_provider:
            try:
                msg = await self._positions_provider()
            except Exception as e:
                logger.error(f"Positions provider error: {e}")
                msg = "⚠️ Error fetching positions"
        else:
            msg = "Positions not available"
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cfg = self._cfg
        mode = "TESTNET" if cfg.USE_TESTNET else "LIVE"
        msg = (
            f"⚙️ *Config*\n"
            f"Mode: `{mode}`\n"
            f"Pairs: {len(cfg.BASE_PAIRS)}\n"
            f"Strategies: {len(cfg.STRATEGIES)}\n"
            f"Risk: `{cfg.MAX_RISK_PER_TRADE*100:.1f}%/trade`\n"
            f"Max DD halt: `{cfg.DRAWDOWN_HALT_THRESHOLD*100:.0f}%`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._report_provider:
            try:
                msg = await self._report_provider()
            except Exception as e:
                logger.error(f"Report provider error: {e}")
                msg = "⚠️ Error fetching report"
        else:
            msg = "Report not available"
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._on_pause:
            try:
                await self._on_pause()
                await update.message.reply_text("⏸️ Paused")
            except Exception as e:
                logger.error(f"Pause error: {e}")
                await update.message.reply_text("⚠️ Error pausing")
        else:
            await update.message.reply_text("Pause not wired")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._on_resume:
            try:
                await self._on_resume()
                await update.message.reply_text("▶️ Resumed")
            except Exception as e:
                logger.error(f"Resume error: {e}")
                await update.message.reply_text("⚠️ Error resuming")
        else:
            await update.message.reply_text("Resume not wired")

    async def _cmd_halt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._on_halt:
            try:
                await self._on_halt()
                await update.message.reply_text("🛑 Emergency halt triggered")
            except Exception as e:
                logger.error(f"Halt error: {e}")
                await update.message.reply_text("⚠️ Error triggering halt")
        else:
            await update.message.reply_text("Halt not wired")

    # ─── Alerts ──────────────────────────────────────────────

    async def send(self, message: str) -> None:
        """Send raw message to Telegram chat."""
        if self._app and self._cfg.TELEGRAM_CHAT_ID:
            try:
                await self._app.bot.send_message(
                    chat_id=self._cfg.TELEGRAM_CHAT_ID,
                    text=message,
                    parse_mode="Markdown",
                )
            except Exception as e:
                # FIX: Log error instead of silent failure
                logger.error(f"Telegram send error: {e}")
        else:
            logger.warning("Telegram not configured — message dropped")

    async def alert_signal(self, sig: Signal) -> None:
        icon = "🟢" if sig.direction == "LONG" else "🔴"
        ist_time = self._get_ist_time(sig.timestamp)
        # FIX: Use ml_score instead of ml_confidence, atr instead of estimated_hold
        msg = (
            f"🚨 *KAVACH-07 SIGNAL*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Pair: {sig.symbol}\n"
            f"🎯 Strategy: {sig.strategy}\n"
            f"{icon} Direction: {sig.direction}\n"
            f"🎲 Confidence: {sig.confidence:.0%}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 ENTRY\n"
            f"`{sig.entry_price:.6g}`\n\n"
            f"✅ TP1\n"
            f"`{sig.tp1_price:.6g}`\n\n"
            f"🛑 SL\n"
            f"`{sig.sl_price:.6g}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📋 R/R: {abs(sig.tp1_price - sig.entry_price) / abs(sig.entry_price - sig.sl_price):.1f}R\n"
            f"⚡ Risk: {sig.risk_pct*100:.2f}%\n"
            f"🤖 ML: {sig.ml_score:.0%}\n"
            f"📊 ATR: {sig.atr:.6g}\n"
            f"🕐 IST: {ist_time}"
        )
        await self.send(msg)

    async def alert_trade_opened(self, pos: Position) -> None:
        ist_time = self._get_ist_time(pos.open_time)
        msg = (
            f"📋 *Position Opened*\n"
            f"{'🟢' if pos.direction == 'LONG' else '🔴'} {pos.symbol} | {pos.direction} | {pos.strategy}\n"
            f"Entry: `{pos.entry_price:.6g}` | Size: {pos.size:.4f}\n"
            f"SL: `{pos.sl_price:.6g}` | TP1: `{pos.tp1_price:.6g}`\n"
            f"🕐 IST: {ist_time}"
        )
        await self.send(msg)

    async def alert_trade_closed(self, result: TradeResult) -> None:
        icon = "✅" if result.pnl > 0 else "🔴"
        ist_time = self._get_ist_time(datetime.now(timezone.utc))
        msg = (
            f"{icon} *Trade Closed — {result.exit_reason}*\n"
            f"{result.symbol} | {result.direction} | {result.strategy}\n"
            f"Entry: `{result.entry_price:.6g}` → Exit: `{result.exit_price:.6g}`\n"
            f"PnL: `{result.pnl:+.4f}` | R: `{result.r_multiple:+.2f}R`\n"
            f"Duration: {result.duration_seconds/3600:.1f}h\n"
            f"🕐 IST: {ist_time}"
        )
        await self.send(msg)

    async def alert_regime(self, regime: RegimeSignal) -> None:
        icon = "🌊" if regime.bias == "NEUTRAL" else ("🐂" if regime.bias == "BULLISH" else "🐻")
        ist_time = self._get_ist_time(regime.timestamp)
        # FIX: Use position_multiplier instead of size_multiplier
        msg = (
            f"📊 *KAVACH-07 REGIME*\n"
            f"{icon} Global Filter: {regime.bias}\n"
            f"Confidence: {regime.confidence:.0%}\n"
            f"Size multiplier: {regime.position_multiplier:.1f}x\n"
            f"🕐 IST: {ist_time}"
        )
        await self.send(msg)

    async def alert_circuit_breaker(self, state: str, reason: str) -> None:
        icon = "🚨" if state == "HALT" else "⚠️"
        ist_time = self._get_ist_time(datetime.now(timezone.utc))
        await self.send(f"{icon} CIRCUIT BREAKER: {state}\n{reason}\n🕐 IST: {ist_time}")

    # ─── Helpers ─────────────────────────────────────────────

    @staticmethod
    def _get_ist_time(ts) -> str:
        """Convert various timestamp types to IST string."""
        if ts is None:
            ts = datetime.now(timezone.utc)
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                # FIX: Specific exceptions instead of bare except
                ts = datetime.now(timezone.utc)
        if isinstance(ts, datetime):
            ist = ts.astimezone(timezone(timedelta(hours=5, minutes=30)))
            return ist.strftime("%Y-%m-%d %H:%M IST")
        return str(ts)
