"""
KAVACH-07 — Alert Manager
Telegram bot integration:
  - Sends MetaSignal alerts with YES/NO inline keyboard
  - /pause, /resume, /status, /trades commands
  - Error and daily status broadcasts
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


class AlertManager:
    """Manages all Telegram interactions for KAVACH-07."""

    def __init__(
        self,
        config: dict,
        db_manager: Any,
        risk_manager: Any,
        bot_token: str,
        chat_id: str,
    ) -> None:
        self._cfg          = config
        self._db           = db_manager
        self._risk         = risk_manager
        self._token        = bot_token
        self._chat_id      = str(chat_id)
        self._app: Optional[Application] = None
        self._pending: Dict[int, dict] = {}   # signal_id → signal dict
        self._on_confirmed: Optional[Callable] = None

    # ─────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Build and start the Telegram Application."""
        try:
            self._app = (
                Application.builder()
                .token(self._token)
                .build()
            )
            self._register_handlers()
            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling(
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=True,
            )
            logger.info("AlertManager: Telegram bot started.")
            await self.send_text("🛡️ *KAVACH\\-07 ONLINE*\nBot started and awaiting signals\\.")
        except TelegramError as exc:
            logger.critical("AlertManager.start failed: %s", exc, exc_info=True)
            raise

    async def stop(self) -> None:
        """Gracefully stop Telegram polling."""
        try:
            if self._app:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
                logger.info("AlertManager stopped.")
        except Exception as exc:
            logger.error("AlertManager.stop error: %s", exc)

    def set_confirm_callback(self, fn: Callable) -> None:
        """Register callback invoked when user confirms a signal via YES button."""
        self._on_confirmed = fn

    # ─────────────────────────────────────────────────────────────────────
    # Signal alerts
    # ─────────────────────────────────────────────────────────────────────

    async def send_signal_alert(self, signal: Any, signal_id: int) -> None:
        """Send a formatted MetaSignal alert with YES/NO inline keyboard."""
        side_emoji = "🟢 LONG" if signal.side == "LONG" else "🔴 SHORT"
        conf_bar   = _confidence_bar(signal.confidence)
        sl_pct     = abs(signal.entry - signal.stop_loss) / max(signal.entry, 1e-10) * 100
        tp_pct     = abs(signal.take_profit - signal.entry) / max(signal.entry, 1e-10) * 100
        rr_ratio   = tp_pct / max(sl_pct, 0.001)

        strats_str = "\\, ".join(
            _esc(s[:20]) for s in signal.strategies_fired[:5]
        )

        text = (
            f"⚡ *KAVACH\\-07 SIGNAL*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 *Symbol:* `{_esc(signal.symbol)}`\n"
            f"📊 *Side:* {side_emoji}\n"
            f"🎯 *Confidence:* `{signal.confidence:.1f}%` {conf_bar}\n"
            f"💰 *Entry:* `{signal.entry:.6g}`\n"
            f"🛑 *Stop Loss:* `{signal.stop_loss:.6g}` \\(\\-{sl_pct:.2f}%\\)\n"
            f"✅ *Take Profit:* `{signal.take_profit:.6g}` \\(\\+{tp_pct:.2f}%\\)\n"
            f"⚖️ *R:R:* `1:{rr_ratio:.2f}`\n"
            f"📐 *Position:* `${signal.position_size_usdt:.2f} USDT`\n"
            f"🌡 *Regime:* `{_esc(signal.regime)}`\n"
            f"🔬 *Strategies:* {strats_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 {_esc(signal.rationale[:200])}"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ YES — Confirm", callback_data=f"yes:{signal_id}"),
                InlineKeyboardButton("❌ NO — Skip",     callback_data=f"no:{signal_id}"),
            ]
        ])
        try:
            self._pending[signal_id] = {"signal": signal, "ts": time.time()}
            await self._app.bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
        except TelegramError as exc:
            logger.error("send_signal_alert error: %s", exc)

    async def send_error_alert(self, component: str, message: str) -> None:
        """Send a critical error notification."""
        text = f"🚨 *KAVACH\\-07 ERROR*\n`{_esc(component)}`\n{_esc(message[:300])}"
        await self.send_text(text)

    async def send_text(self, text: str) -> None:
        """Send a plain MarkdownV2 message."""
        try:
            if self._app and self._app.bot:
                await self._app.bot.send_message(
                    chat_id=self._chat_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
        except TelegramError as exc:
            logger.error("send_text error: %s", exc)

    async def send_status_update(self, risk_status: dict, open_trades: list) -> None:
        """Periodic status broadcast."""
        paused_str  = "⏸ PAUSED" if risk_status.get("paused") else "▶️ RUNNING"
        daily_pnl   = risk_status.get("daily_pnl", 0.0)
        pnl_emoji   = "🟢" if daily_pnl >= 0 else "🔴"
        hours_str   = "✅ IN WINDOW" if risk_status.get("within_hours") else "⏰ OUT OF HOURS"
        open_count  = len(open_trades)

        text = (
            f"📊 *KAVACH\\-07 STATUS*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🤖 Status: {_esc(paused_str)}\n"
            f"⏰ Hours: {_esc(hours_str)}\n"
            f"{pnl_emoji} Daily PnL: `{daily_pnl:+.4f} USDT`\n"
            f"📈 Open Trades: `{open_count}`\n"
        )
        await self.send_text(text)

    # ─────────────────────────────────────────────────────────────────────
    # Command & callback handlers
    # ─────────────────────────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        app = self._app
        app.add_handler(CommandHandler("start",   self._cmd_start))
        app.add_handler(CommandHandler("pause",   self._cmd_pause))
        app.add_handler(CommandHandler("resume",  self._cmd_resume))
        app.add_handler(CommandHandler("status",  self._cmd_status))
        app.add_handler(CommandHandler("trades",  self._cmd_trades))
        app.add_handler(CommandHandler("signals", self._cmd_signals))
        app.add_handler(CallbackQueryHandler(self._handle_callback))

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorised(update):
            return
        await update.message.reply_text(
            "🛡️ KAVACH-07 is active.\n"
            "Commands: /pause /resume /status /trades /signals"
        )

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorised(update):
            return
        self._risk.pause()
        await update.message.reply_text("⏸ Bot PAUSED. No new signals will be processed.")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorised(update):
            return
        self._risk.resume()
        await update.message.reply_text("▶️ Bot RESUMED.")

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorised(update):
            return
        try:
            open_trades  = await self._db.get_open_trades()
            risk_status  = self._risk.status_dict()
            daily_pnl    = risk_status.get("daily_pnl", 0.0)
            paused       = risk_status.get("paused", False)
            open_count   = len(open_trades)
            text = (
                f"🛡 KAVACH-07 STATUS\n"
                f"{'⏸ PAUSED' if paused else '▶️ RUNNING'}\n"
                f"Daily PnL: {daily_pnl:+.4f} USDT\n"
                f"Open trades: {open_count}\n"
                f"In trading hours: {'Yes' if risk_status.get('within_hours') else 'No'}"
            )
            await update.message.reply_text(text)
        except Exception as exc:
            await update.message.reply_text(f"Error: {exc}")

    async def _cmd_trades(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorised(update):
            return
        try:
            trades = await self._db.get_open_trades()
            if not trades:
                await update.message.reply_text("No open trades.")
                return
            lines = ["📊 OPEN TRADES:"]
            for t in trades[:10]:
                pnl_est = ""
                lines.append(
                    f"• {t.get('symbol','?')} {t.get('side','?')} "
                    f"@ {t.get('entry_price',0):.4f}"
                )
            await update.message.reply_text("\n".join(lines))
        except Exception as exc:
            await update.message.reply_text(f"Error: {exc}")

    async def _cmd_signals(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorised(update):
            return
        try:
            signals = await self._db.get_latest_signals(limit=5)
            if not signals:
                await update.message.reply_text("No recent signals.")
                return
            lines = ["⚡ RECENT SIGNALS (last 5):"]
            for s in signals:
                lines.append(
                    f"• {s.get('symbol','?')} {s.get('side','?')} "
                    f"conf={s.get('confidence',0):.1f}% "
                    f"{'✅' if s.get('confirmed') else '❌'}"
                )
            await update.message.reply_text("\n".join(lines))
        except Exception as exc:
            await update.message.reply_text(f"Error: {exc}")

    async def _handle_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle YES/NO inline keyboard callbacks."""
        query = update.callback_query
        await query.answer()
        if not self._is_authorised(update):
            return

        data = query.data or ""
        try:
            action, signal_id_str = data.split(":", 1)
            signal_id = int(signal_id_str)
        except (ValueError, AttributeError):
            logger.warning("Bad callback data: %s", data)
            return

        pending = self._pending.pop(signal_id, None)
        if action == "yes":
            try:
                await self._db.confirm_signal(signal_id)
                sig = pending["signal"] if pending else None
                entry = sig.entry if sig else 0.0
                trade_id = await self._db.insert_trade(signal_id, entry)
                await query.edit_message_text(
                    f"✅ Trade CONFIRMED (ID={trade_id})\n"
                    f"Signal {signal_id} logged to DB.\n"
                    f"Entry: {entry:.6g}"
                )
                if self._on_confirmed and sig:
                    await self._on_confirmed(signal_id, trade_id, sig)
            except Exception as exc:
                logger.error("confirm callback error: %s", exc)
                await query.edit_message_text(f"⚠️ Confirmation error: {exc}")
        elif action == "no":
            await query.edit_message_text(f"❌ Signal {signal_id} skipped.")

    def _is_authorised(self, update: Update) -> bool:
        """Only respond to the configured chat_id."""
        try:
            chat = update.effective_chat
            user = update.effective_user
            # Accept messages from configured chat_id
            ok = str(chat.id) == self._chat_id if chat else False
            if not ok:
                logger.warning(
                    "Unauthorised access attempt from chat_id=%s user=%s",
                    chat.id if chat else "?", user.id if user else "?",
                )
            return ok
        except Exception:
            return False

    @property
    def is_paused(self) -> bool:
        return self._risk.is_paused


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _confidence_bar(conf: float) -> str:
    """ASCII confidence progress bar."""
    filled = int(conf / 10)
    return "█" * filled + "░" * (10 - filled)


def _esc(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    special = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))
