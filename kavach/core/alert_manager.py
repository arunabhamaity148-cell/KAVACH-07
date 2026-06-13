"""
KAVACH-07 — Alert Manager
Handles Telegram bot interactions, formatting, and sentiment-direction safety guards.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    Application,
)

logger = logging.getLogger("kavach.alert_manager")

class AlertManager:
    """
    Manages Telegram communication and enforces directional sentiment safety.
    """

    def __init__(
        self, 
        config: dict, 
        db_manager: Any, 
        risk_manager: Any, 
        token: str, 
        chat_id: str
    ):
        self._cfg = config
        self._db = db_manager
        self._risk = risk_manager
        self._token = token
        self._chat_id = chat_id
        
        self._app: Optional[Application] = None
        self._on_confirm_callback: Optional[Callable] = None
        
        # Internal cache for signals awaiting confirmation
        # Key: signal_id (str), Value: MetaSignal object
        self._pending_signals: Dict[str, Any] = {}

    async def start(self) -> None:
        """Initializes and starts the Telegram bot."""
        if not self._token or not self._chat_id:
            logger.error("Telegram credentials missing. Alert Manager disabled.")
            return

        self._app = ApplicationBuilder().token(self._token).build()

        # Command Handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        self._app.add_handler(CommandHandler("sentiment", self._cmd_sentiment))
        
        # Button Callbacks
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        
        logger.info("Alert Manager: Telegram bot active for Chat ID: %s", self._chat_id)
        await self.send_text("🛡️ *KAVACH-07 ONLINE*\nSystem initialized and monitoring markets.")

    async def stop(self) -> None:
        """Stops the bot."""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        logger.info("Alert Manager stopped")

    def set_confirm_callback(self, callback: Callable) -> None:
        """Sets the function to call when a user clicks 'YES'."""
        self._on_confirm_callback = callback

    # ──────────────────────────────────────────────────────────────────────────
    # Core Alert Logic
    # ──────────────────────────────────────────────────────────────────────────

    async def send_signal_alert(self, signal: Any, signal_id: int) -> None:
        """
        Formats and sends a MetaSignal alert with a Sentiment-Direction Guard.
        """
        # 1. Sentiment-Direction Guard
        # If BULLISH sentiment + SHORT signal (or vice versa) -> BLOCK
        if not self._check_sentiment_guard(signal):
            logger.warning("SENTIMENT_MISMATCH: %s %s signal blocked by guard.", signal.symbol, signal.side)
            await self._db.log_event("WARNING", "sentiment_guard", f"Blocked {signal.side} on {signal.symbol} due to sentiment conflict.")
            return

        # 2. Formatting
        side_emoji = "🟢 LONG" if signal.side == "LONG" else "🔴 SHORT"
        conf_stars = "⭐" * int(round(signal.confidence / 20))
        
        text = (
            f"⚡ *NEW SIGNAL: {signal.symbol}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *Side:* {side_emoji}\n"
            f"🎯 *Confidence:* {signal.confidence}% {conf_stars}\n"
            f"💰 *Entry:* `{signal.entry:.6g}`\n"
            f"🛑 *Stop Loss:* `{signal.stop_loss:.6g}`\n"
            f"✅ *Take Profit:* `{signal.take_profit:.6g}`\n"
            f"📐 *Size:* `${signal.position_size_usdt:.2f}`\n"
            f"🌡️ *Regime:* `{signal.regime}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔬 *Logic:* {self._escape_markdown(signal.rationale)}\n"
            f"🤖 *Strategies:* {', '.join(signal.strategies_fired)}"
        )

        # 3. Buttons
        keyboard = [
            [
                InlineKeyboardButton("✅ CONFIRM (YES)", callback_data=f"confirm_{signal_id}"),
                InlineKeyboardButton("❌ REJECT (NO)", callback_data=f"reject_{signal_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # 4. Cache and Send
        self._pending_signals[str(signal_id)] = signal
        await self._app.bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup
        )

    async def send_text(self, text: str) -> None:
        """Sends a plain markdown message."""
        if not self._app: return
        try:
            await self._app.bot.send_message(
                chat_id=self._chat_id, 
                text=self._escape_markdown(text), 
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            logger.error("Failed to send telegram message: %s", e)

    # ──────────────────────────────────────────────────────────────────────────
    # Handlers
    # ──────────────────────────────────────────────────────────────────────────

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        
        if str(query.message.chat.id) != self._chat_id:
            return

        data = query.data
        if not data: return

        action, signal_id = data.split("_")
        signal = self._pending_signals.get(signal_id)

        if action == "confirm":
            if signal and self._on_confirm_callback:
                await query.edit_message_text(
                    text=query.message.text + "\n\n✅ *EXECUTING...*",
                    parse_mode=None # Use raw text to preserve existing content
                )
                # Execute trade via main loop callback
                await self._on_confirm_callback(int(signal_id), signal)
                self._pending_signals.pop(signal_id, None)
            else:
                await query.edit_message_text("⚠️ Error: Signal expired or executor missing.")

        elif action == "reject":
            await query.edit_message_text(
                text=query.message.text + "\n\n❌ *REJECTED BY USER*",
                parse_mode=None
            )
            self._pending_signals.pop(signal_id, None)

    # ──────────────────────────────────────────────────────────────────────────
    # Commands
    # ──────────────────────────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("🛡️ KAVACH-07 v7.0.0 Online.")

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._risk.pause()
        await update.message.reply_text("⏸ Bot manually PAUSED.")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._risk.resume()
        await update.message.reply_text("▶️ Bot manually RESUMED.")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        status = "⏸ PAUSED" if self._risk.is_paused else "▶️ RUNNING"
        open_trades = await self._db.get_open_trades()
        text = (
            f"🛡️ *KAVACH-07 STATUS*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🤖 *Bot:* {status}\n"
            f"📈 *Open Trades:* {len(open_trades)}\n"
            f"💰 *Daily PnL:* `${self._risk._daily_pnl:.2f}`"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        daily = await self._db.get_daily_pnl()
        total = await self._db.get_total_pnl()
        text = (
            f"💰 *PNL REPORT*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📅 *Today:* `${daily:+.2f}`\n"
            f"🌍 *Total:* `${total:+.2f}`"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

    async def _cmd_sentiment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._risk._news_engine:
            status = self._risk._news_engine.get_status()
            text = (
                f"🌡️ *MARKET SENTIMENT*\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📊 *Score:* `{status['score']:.1f}`\n"
                f"💥 *Impact:* `{status['impact']}`\n"
                f"📰 *Source:* `{status['dominant_source']}`"
            )
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text("News Engine not active.")

    # ──────────────────────────────────────────────────────────────────────────
    # Utils
    # ──────────────────────────────────────────────────────────────────────────

    def _check_sentiment_guard(self, signal: Any) -> bool:
        """
        Enforces: BULLISH sentiment + SHORT signal (or vice versa) -> BLOCK.
        """
        if not self._risk._news_engine:
            return True
            
        sentiment = self._risk._news_engine.get_status()["score"]
        
        # Bullish threshold > 3.0, Bearish threshold < -3.0
        if sentiment >= 3.0 and signal.side == "SHORT":
            return False
        if sentiment <= -3.0 and signal.side == "LONG":
            return False
            
        return True

    def _escape_markdown(self, text: str) -> str:
        """Escapes special characters for Telegram MarkdownV2."""
        escape_chars = r"_*[]()~`>#+-=|{}.!"
        return "".join(f"\\{c}" if c in escape_chars else c for c in text)