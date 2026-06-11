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

_MAX_MSG_LEN  = 4096
_RETRY_DELAY  = 3      # seconds before retrying failed send
_MAX_RETRIES  = 5
_RATE_DELAY   = 0.5    # min seconds between messages


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
            ("start",     self._cmd_start),
            ("status",    self._cmd_status),
            ("balance",   self._cmd_balance),
            ("signals",   self._cmd_signals),
            ("trades",    self._cmd_trades),
            ("positions", self._cmd_positions),
            ("pause",     self._cmd_pause),
            ("resume",    self._cmd_resume),
            ("halt",      self._cmd_halt),
            ("report",    self._cmd_report),
            ("config",    self._cmd_config),
        ]
        for name, handler in handlers:
            self._app.add_handler(CommandHandler(name, handler))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

        await self.send("🚀 <b>KAVACH-07 ONLINE</b>\nSignal bot started successfully.", parse_md=True)
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

    async def send(self, text: str, parse_md: bool = False) -> None:
        """Send a message with retry and rate limiting."""
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
                        parse_mode=ParseMode.HTML if parse_md else None,
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
        """Format and send a signal alert."""
        icon = "🚨" if sig.strategy not in ("OB_IMBALANCE", "EXCHANGE_ARB") else "⚡"
        if sig.strategy == "OI_BREAKOUT":
            icon = "🚀"

        dir_icon = "🟢" if sig.direction == "LONG" else "🔴"
        tp2_line = f"\nTP2: <code>{sig.tp2_price:.6g}</code>" if sig.tp2_price else ""

        text = (
            f"{icon} <b>KAVACH-07 SIGNAL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Pair:</b> <code>{sig.symbol}</code>\n"
            f"<b>Strategy:</b> <code>{sig.strategy}</code>\n"
            f"<b>Direction:</b> {dir_icon} <code>{sig.direction}</code>\n"
            f"<b>Confidence:</b> <code>{sig.confidence*100:.0f}%</code>\n"
            f"\n"
            f"<b>Entry:</b> <code>{sig.entry_price:.6g}</code> ({sig.entry_type})\n"
            f"<b>SL:</b> <code>{sig.sl_price:.6g}</code>\n"
            f"<b>TP1:</b> <code>{sig.tp1_price:.6g}</code> ({sig.r_ratio:.1f}R){tp2_line}\n"
            f"\n"
            f"<b>Rationale:</b>\n{sig.rationale}\n"
            f"\n"
            f"<b>Risk:</b> <code>{sig.risk_pct*100:.2f}%</code> | <b>ML:</b> <code>{sig.ml_score*100:.0f}%</code>\n"
            f"<b>Time:</b> <code>{sig.timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC</code>"
        )
        await self.send(text, parse_md=True)

    async def alert_trade_opened(self, pos: Position) -> None:
        dir_icon = "🟢" if pos.direction == "LONG" else "🔴"
        text = (
            f"📋 <b>Position Opened</b>\n"
            f"{dir_icon} {pos.symbol} | {pos.direction} | {pos.strategy}\n"
            f"Entry: <code>{pos.entry_price:.6g}</code> | Size: <code>{pos.size:.4g}</code>\n"
            f"SL: <code>{pos.sl_price:.6g}</code> | TP1: <code>{pos.tp1_price:.6g}</code>"
        )
        await self.send(text, parse_md=True)

    async def alert_trade_closed(self, result: TradeResult) -> None:
        pnl_icon = "✅" if result.pnl > 0 else "❌"
        text = (
            f"{pnl_icon} <b>Trade Closed — {result.exit_reason}</b>\n"
            f"{result.symbol} | {result.direction} | {result.strategy}\n"
            f"Entry: <code>{result.entry_price:.6g}</code> → Exit: <code>{result.exit_price:.6g}</code>\n"
            f"PnL: <code>{result.pnl:+.4f}</code> | R: <code>{result.r_multiple:+.2f}R</code>\n"
            f"Duration: <code>{result.duration_seconds/3600:.1f}h</code>"
        )
        await self.send(text, parse_md=True)

    async def alert_regime(self, regime: RegimeSignal) -> None:
        icon = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(regime.bias, "⚪")
        text = (
            f"📊 <b>KAVACH-07 REGIME</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Global Filter: {icon} <code>{regime.bias}</code>\n"
            f"Confidence: <code>{regime.confidence*100:.0f}%</code>\n"
            f"\n"
            f"<b>Rationale:</b>\n"
            f"• Avg Funding: <code>{regime.avg_funding*100:.4f}%</code>\n"
            f"• OI Trend: <code>{regime.oi_trend*100:+.1f}%</code>\n"
            f"\n"
            f"Impact: Size multiplier <code>{regime.position_multiplier:.1f}x</code>\n"
            f"Time: <code>{regime.timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC</code>"
        )
        await self.send(text, parse_md=True)

    async def alert_circuit_breaker(self, state: str, reason: str) -> None:
        icon = "🚨" if state == "HALT" else "⚠️"
        await self.send(f"{icon} <b>CIRCUIT BREAKER: {state}</b>\n{reason}", parse_md=True)

    # ─── Command handlers ─────────────────────────────────────

    def _auth(self, update: Update) -> bool:
        return str(update.effective_chat.id) == str(self._chat_id)

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        text = (
            "🤖 <b>KAVACH COMMANDS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "/status — Bot health\n"
            "/pnl — Daily P&amp;L\n"
            "/positions — Open trades\n"
            "/history — Trade history\n"
            "/stop — Emergency stop\n"
            "/resume — Resume trading\n"
            "/config — View settings\n"
            "/help — This message"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        if self._status_provider:
            text = await self._status_provider()
        else:
            text = "Status provider not registered"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_balance(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        if self._balance_provider:
            text = await self._balance_provider()
        else:
            text = "Balance provider not registered"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_signals(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        sigs = await self._db.get_recent_signals(limit=5)
        if not sigs:
            await update.message.reply_text("No signals yet.")
            return
        lines = ["📡 <b>Last 5 Signals</b>\n"]
        for s in sigs:
            ts = datetime.fromtimestamp(s["timestamp"] / 1000, tz=timezone.utc)
            lines.append(
                f"• {s['symbol']} {s['direction']} | {s['strategy']}\n"
                f"  Conf: {s['confidence']*100:.0f}% | "
                f"{ts.strftime('%m-%d %H:%M')} UTC\n"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_trades(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        trades = await self._db.get_recent_trades(limit=5)
        if not trades:
            await update.message.reply_text("No trades yet.")
            return
        lines = ["📊 <b>Last 5 Trades</b>\n"]
        for t in trades:
            pnl_icon = "✅" if t["pnl"] > 0 else "❌"
            lines.append(
                f"{pnl_icon} {t['symbol']} {t['direction']} | "
                f"PnL: {t['pnl']:+.4f} | {t['exit_reason']}\n"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        if self._positions_provider:
            text = await self._positions_provider()
        else:
            text = "Positions provider not registered"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

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
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text("Report provider not registered")

    async def _cmd_config(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        cfg = self._cfg
        text = (
            f"⚙️ <b>KAVACH-07 Config</b>\n"
            f"Mode: <code>{'TESTNET' if cfg.USE_TESTNET else 'LIVE'}</code>\n"
            f"Pairs: <code>{len(cfg.BASE_PAIRS)}</code>\n"
            f"Strategies: <code>{len(cfg.STRATEGIES)}</code>\n"
            f"Risk/trade: <code>{cfg.MAX_RISK_PER_TRADE*100:.2f}%</code>\n"
            f"Max exposure: <code>{cfg.MAX_TOTAL_EXPOSURE*100:.1f}%</code>\n"
            f"ML threshold: <code>{cfg.ML_CONFIDENCE_THRESHOLD}</code>\n"
            f"Scan interval: <code>{cfg.SCAN_INTERVAL}s</code>"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    # ─── Callback registration ────────────────────────────────

    def register_handlers(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, f"_{k}", v)
