"""
KAVACH-07 — Telegram Bot
Signal alerts, regime updates, commands (/status /balance /signals etc).
Async. Retry logic. Rate-limited to avoid flooding.
"""
from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timezone
from typing import Optional

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes

from config import Config
from database import Database
from models import Position, RegimeSignal, Signal, TradeResult
from utils import get_logger

logger = get_logger(__name__)

_MAX_MSG_LEN = 4096
_RETRY_DELAY = 3  # seconds before retrying failed send
_MAX_RETRIES = 5
_RATE_DELAY = 0.5  # min seconds between messages


class TelegramBot:

    def __init__(self, config: Config, db: Database):
        self._cfg = config
        self._db = db
        self._bot: Optional[Bot] = None
        self._app: Optional[Application] = None
        self._chat_id = config.TELEGRAM_CHAT_ID
        self._enabled = bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)

        self._last_send: float = 0.0
        self._send_lock = asyncio.Lock()

        self._on_pause: Optional[callable] = None
        self._on_resume: Optional[callable] = None
        self._on_halt: Optional[callable] = None
        self._status_provider: Optional[callable] = None
        self._balance_provider: Optional[callable] = None
        self._signals_provider: Optional[callable] = None
        self._trades_provider: Optional[callable] = None
        self._positions_provider: Optional[callable] = None
        self._config_provider: Optional[callable] = None
        self._report_provider: Optional[callable] = None

    # ─── Lifecycle ───────────────────────────────────────────

    async def start(self) -> None:
        if not self._enabled:
            logger.warning("Telegram not configured — alerts disabled")
            return

        self._app = (
            Application.builder()
            .token(self._cfg.TELEGRAM_BOT_TOKEN)
            .build()
        )
        self._bot = self._app.bot

        handlers = [
            ("start", self._cmd_start),
            ("status", self._cmd_status),
            ("balance", self._cmd_balance),
            ("signals", self._cmd_signals),
            ("trades", self._cmd_trades),
            ("positions", self._cmd_positions),
            ("pause", self._cmd_pause),
            ("resume", self._cmd_resume),
            ("halt", self._cmd_halt),
            ("report", self._cmd_report),
            ("config", self._cmd_config),
        ]
        for name, handler in handlers:
            self._app.add_handler(CommandHandler(name, handler))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

        await self.send("🚀 KAVACH-07 ONLINE\nSignal bot started successfully.")
        logger.info("Telegram bot started")

    async def stop(self) -> None:
        if not self._enabled or not self._app:
            return
        try:
            await self.send("🛑 KAVACH-07 shutting down.")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        except Exception:
            pass

    # ─── Alert senders ───────────────────────────────────────

    async def send(self, text: str) -> None:
        """Send a plain text message with retry and rate limiting."""
        if not self._enabled or not self._bot:
            return

        async with self._send_lock:
            elapsed = asyncio.get_event_loop().time() - self._last_send
            if elapsed < _RATE_DELAY:
                await asyncio.sleep(_RATE_DELAY - elapsed)

            if len(text) > _MAX_MSG_LEN:
                text = text[:_MAX_MSG_LEN - 20] + "\n...[truncated]"

            for attempt in range(1, _MAX_RETRIES + 1):
                try:
                    await self._bot.send_message(
                        chat_id=self._chat_id,
                        text=text,
                        parse_mode=None,  # Plain text for easy copy
                        disable_web_page_preview=True,
                    )
                    self._last_send = asyncio.get_event_loop().time()
                    return
                except RetryAfter as e:
                    logger.warning(f"Telegram rate limited — waiting {e.retry_after}s")
                    await asyncio.sleep(e.retry_after + 1)
                except (NetworkError, TimedOut) as e:
                    if attempt < _MAX_RETRIES:
                        await asyncio.sleep(_RETRY_DELAY * attempt)
                    else:
                        logger.error(f"Telegram send failed after {_MAX_RETRIES} retries: {e}")
                except Exception as e:
                    logger.error(f"Telegram unexpected error: {e}")
                    return

    async def alert_signal(self, sig: Signal) -> None:
        """Premium signal alert — easy copy-paste for CoinDCX"""
        dir_icon = "🟢" if sig.direction == "LONG" else "🔴"

        text = (
            f"🚨 KAVACH-07 SIGNAL\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Pair: {sig.symbol}\n"
            f"🎯 Strategy: {sig.strategy}\n"
            f"{dir_icon} Direction: {sig.direction}\n"
            f"🎲 Confidence: {sig.confidence*100:.0f}%\n"
            f"\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 ENTRY\n"
            f"{sig.entry_price:.6g}\n"
            f"\n"
            f"✅ TP1\n"
            f"{sig.tp1_price:.6g}\n"
            f"\n"
            f"🛑 SL\n"
            f"{sig.sl_price:.6g}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"📋 R/R: {sig.r_ratio:.1f}R\n"
            f"⚡ Risk: {sig.risk_pct*100:.2f}%\n"
            f"🤖 ML: {sig.ml_score*100:.0f}%\n"
            f"⏰ {sig.timestamp.strftime('%Y-%m-%d %H:%M')} IST"
        )
        await self.send(text)

    async def alert_trade_opened(self, pos: Position) -> None:
        dir_icon = "🟢" if pos.direction == "LONG" else "🔴"
        text = (
            f"📋 Position Opened\n"
            f"{dir_icon} {pos.symbol} | {pos.direction} | {pos.strategy}\n"
            f"Entry: {pos.entry_price:.6g} | Size: {pos.size:.4g}\n"
            f"SL: {pos.sl_price:.6g} | TP1: {pos.tp1_price:.6g}"
        )
        await self.send(text)

    async def alert_trade_closed(self, result: TradeResult) -> None:
        pnl_icon = "✅" if result.pnl > 0 else "❌"
        text = (
            f"{pnl_icon} Trade Closed — {result.exit_reason}\n"
            f"{result.symbol} | {result.direction} | {result.strategy}\n"
            f"Entry: {result.entry_price:.6g} → Exit: {result.exit_price:.6g}\n"
            f"PnL: {result.pnl:+.4f} | R: {result.r_multiple:+.2f}R\n"
            f"Duration: {result.duration_seconds/3600:.1f}h"
        )
        await self.send(text)

    async def alert_regime(self, regime: RegimeSignal) -> None:
        icon = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(regime.bias, "⚪")
        text = (
            f"📊 KAVACH-07 REGIME\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Global Filter: {icon} {regime.bias}\n"
            f"Confidence: {regime.confidence*100:.0f}%\n"
            f"\n"
            f"Rationale:\n"
            f"• Avg Funding: {regime.avg_funding*100:.4f}%\n"
            f"• OI Trend: {regime.oi_trend*100:+.1f}%\n"
            f"\n"
            f"Impact: Size multiplier {regime.position_multiplier:.1f}x\n"
            f"Time: {regime.timestamp.strftime('%Y-%m-%d %H:%M')} IST"
        )
        await self.send(text)

    async def alert_circuit_breaker(self, state: str, reason: str) -> None:
        icon = "🚨" if state == "HALT" else "⚠️"
        await self.send(f"{icon} CIRCUIT BREAKER: {state}\n{reason}")

    # ─── Command handlers ─────────────────────────────────────

    def _auth(self, update: Update) -> bool:
        return str(update.effective_chat.id) == str(self._chat_id)

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        text = (
            "🤖 KAVACH-07 COMMANDS\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "/status — Bot health & circuit state\n"
            "/balance — P&L & balance\n"
            "/positions — Open trades\n"
            "/signals — Last 5 signals\n"
            "/trades — Last 5 closed trades\n"
            "/report — Hourly performance report\n"
            "/pause — Pause signal generation\n"
            "/resume — Resume (clears halt too)\n"
            "/halt — Emergency stop all\n"
            "/config — View settings"
        )
        await update.message.reply_text(text)

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        if self._status_provider:
            text = await self._status_provider()
        else:
            text = "Status provider not registered"
        await update.message.reply_text(text)

    async def _cmd_balance(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        if self._balance_provider:
            text = await self._balance_provider()
        else:
            text = "Balance provider not registered"
        await update.message.reply_text(text)

    async def _cmd_signals(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        sigs = await self._db.get_recent_signals(limit=5)
        if not sigs:
            await update.message.reply_text("No signals yet.")
            return
        lines = ["📡 Last 5 Signals\n"]
        for s in sigs:
            ts = datetime.fromtimestamp(s["timestamp"] / 1000, tz=timezone.utc)
            lines.append(
                f"• {s['symbol']} {s['direction']} | {s['strategy']}\n"
                f"  Conf: {s['confidence']*100:.0f}% | "
                f"{ts.strftime('%m-%d %H:%M')} UTC\n"
            )
        await update.message.reply_text("\n".join(lines))

    async def _cmd_trades(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        trades = await self._db.get_recent_trades(limit=5)
        if not trades:
            await update.message.reply_text("No trades yet.")
            return
        lines = ["📊 Last 5 Trades\n"]
        for t in trades:
            pnl_icon = "✅" if t["pnl"] > 0 else "❌"
            lines.append(
                f"{pnl_icon} {t['symbol']} {t['direction']} | "
                f"PnL: {t['pnl']:+.4f} | {t['exit_reason']}\n"
            )
        await update.message.reply_text("\n".join(lines))

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        if self._positions_provider:
            try:
                text = await self._positions_provider()
                await update.message.reply_text(text)
            except Exception as e:
                logger.error(f"Positions command error: {e}")
                await update.message.reply_text(f"Error fetching positions: {str(e)}")
        else:
            await update.message.reply_text("Positions provider not registered")

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        if self._on_pause:
            await self._on_pause()
            await update.message.reply_text("⏸️ Signal generation paused.")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        if self._on_resume:
            await self._on_resume()
            await update.message.reply_text("▶️ Signal generation resumed.")

    async def _cmd_halt(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        if self._on_halt:
            await self._on_halt()
            await update.message.reply_text(
                "🚨 Emergency halt activated. Use /resume to re-enable."
            )

    async def _cmd_report(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        if self._report_provider:
            text = await self._report_provider()
            await update.message.reply_text(text)
        else:
            await update.message.reply_text("Report provider not registered")

    async def _cmd_config(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        cfg = self._cfg
        text = (
            f"⚙️ KAVACH-07 Config\n"
            f"Mode: {'TESTNET' if cfg.USE_TESTNET else 'LIVE'}\n"
            f"Pairs: {len(cfg.BASE_PAIRS)}\n"
            f"Strategies: {len(cfg.STRATEGIES)}\n"
            f"Risk/trade: {cfg.MAX_RISK_PER_TRADE*100:.2f}%\n"
            f"Max exposure: {cfg.MAX_TOTAL_EXPOSURE*100:.1f}%\n"
            f"ML threshold: {cfg.ML_CONFIDENCE_THRESHOLD}\n"
            f"Scan interval: {cfg.SCAN_INTERVAL}s"
        )
        await update.message.reply_text(text)

    # ─── Callback registration ────────────────────────────────

    def register_handlers(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, f"_{k}", v)
