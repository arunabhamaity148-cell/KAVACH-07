"""
KAVACH-07 — Risk Manager
Enforces all risk controls before a MetaSignal is converted to an actionable alert.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import pytz

from ..strategies.base import MetaSignal

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


class RiskManager:
    """Stateful risk controller.

    Checks (in order):
    1. Paused state
    2. Trading hours (IST window)
    3. Regulatory FUD pairs
    4. Daily loss limit
    5. Max open trades
    6. Correlation groups
    7. Min confidence threshold

    Approved signals get ``position_size_usdt`` calculated.
    """

    def __init__(self, config: dict, db_manager: Any) -> None:
        self._cfg    = config
        self._db     = db_manager
        self._rcfg   = config.get("risk", {})
        self._tcfg   = config.get("trading_hours", {})
        self._lock   = asyncio.Lock()

        # Risk parameters
        self._account_size     = float(self._rcfg.get("account_size_usdt", 5000.0))
        self._max_risk_pct     = float(self._rcfg.get("max_risk_per_trade_percent", 3.0))
        self._max_daily_loss_p = float(self._rcfg.get("max_daily_loss_percent", 5.0))
        self._max_open_trades  = int(self._rcfg.get("max_open_trades", 5))
        self._min_confidence   = float(self._rcfg.get("min_signal_confidence", 62.0))
        self._fud_pairs: Set[str] = set(self._rcfg.get("regulatory_fud_pairs", []))
        self._corr_groups: List[List[str]] = self._rcfg.get("correlation_groups", [])

        # Runtime state
        self._paused: bool         = False
        self._daily_pnl: float     = 0.0
        self._daily_reset_ts: float = 0.0  # Set to 0 to trigger sync on first tick
        self._open_symbols: Set[str] = set()  # symbols with open trades

    # ─────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────

    def pause(self) -> None:
        self._paused = True
        logger.warning("RiskManager: BOT PAUSED.")

    def resume(self) -> None:
        self._paused = False
        logger.info("RiskManager: Bot RESUMED.")

    @property
    def is_paused(self) -> bool:
        return self._paused

    async def filter_signal(
        self, signal: MetaSignal, data_context: Dict[str, Any]
    ) -> Optional[MetaSignal]:
        """Apply all risk checks. Returns enriched signal if approved, else None."""

        # 1. Paused
        if self._paused:
            logger.debug("RiskManager: signal %s rejected — bot paused.", signal.symbol)
            return None

        # 2. NEUTRAL signal → skip
        if signal.side == "NEUTRAL":
            return None

        # 3. Regulatory FUD
        if signal.symbol in self._fud_pairs:
            logger.info("RiskManager: %s rejected — regulatory FUD list.", signal.symbol)
            return None

        # 4. Trading hours (IST)
        if not self._within_trading_hours():
            logger.debug("RiskManager: %s rejected — outside trading hours.", signal.symbol)
            return None

        # 5. Minimum confidence
        if signal.confidence < self._min_confidence:
            logger.debug(
                "RiskManager: %s rejected — confidence %.1f < %.1f",
                signal.symbol, signal.confidence, self._min_confidence,
            )
            return None

        async with self._lock:
            # 6. Refresh daily PnL from DB if reset needed
            await self._maybe_reset_daily_pnl()

            # 7. Daily loss limit
            max_daily_loss = self._account_size * self._max_daily_loss_p / 100.0
            if self._daily_pnl <= -max_daily_loss:
                logger.warning(
                    "RiskManager: Daily loss limit reached (%.2f USDT). Bot pausing.",
                    self._daily_pnl,
                )
                self.pause()
                return None

            # 8. Max open trades
            open_trades = await self._db.get_open_trades()
            open_count  = len(open_trades)
            if open_count >= self._max_open_trades:
                logger.debug(
                    "RiskManager: %s rejected — max open trades (%d) reached.",
                    signal.symbol, self._max_open_trades,
                )
                return None

            # 9. Correlation group — only one pair per group
            currently_open_symbols = {t.get("symbol", "") for t in open_trades}
            if self._is_correlated_open(signal.symbol, currently_open_symbols):
                logger.debug(
                    "RiskManager: %s rejected — correlated pair already trading.",
                    signal.symbol,
                )
                return None

        # 10. Position sizing
        position_size = self._calc_position_size(signal)
        if position_size <= 0:
            logger.warning("RiskManager: position size = 0 for %s — rejected.", signal.symbol)
            return None

        signal.position_size_usdt = position_size
        logger.info(
            "RiskManager: APPROVED %s %s conf=%.1f size=%.2f USDT",
            signal.symbol, signal.side, signal.confidence, position_size,
        )
        return signal

    async def update_daily_pnl(self, pnl_delta: float) -> None:
        """Call this when a trade closes to update today's realised PnL."""
        async with self._lock:
            self._daily_pnl += pnl_delta
            logger.debug("Daily PnL updated: %.4f USDT (total: %.4f)", pnl_delta, self._daily_pnl)

    # ─────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────

    def _within_trading_hours(self) -> bool:
        """Check if current IST time is within the configured trading window."""
        now_ist = datetime.now(IST)
        start_h = int(self._tcfg.get("start_hour", 9))
        start_m = int(self._tcfg.get("start_minute", 30))
        end_h   = int(self._tcfg.get("end_hour", 23))
        end_m   = int(self._tcfg.get("end_minute", 30))

        start_minutes = start_h * 60 + start_m
        end_minutes   = end_h   * 60 + end_m
        now_minutes   = now_ist.hour * 60 + now_ist.minute

        return start_minutes <= now_minutes <= end_minutes

    def _calc_position_size(self, signal: MetaSignal) -> float:
        """Risk-adjusted position size.

        Formula:
            risk_amount  = account_size × max_risk_pct% × (confidence / 100)
            position_size = risk_amount / sl_distance_pct
        Caps at account_size × max_risk_pct% even with 100% confidence.
        """
        if signal.entry <= 0 or signal.stop_loss <= 0:
            return 0.0

        sl_distance_pct = abs(signal.entry - signal.stop_loss) / signal.entry
        if sl_distance_pct < 0.0001:
            return 0.0

        base_risk   = self._account_size * self._max_risk_pct / 100.0
        conf_factor = min(1.0, signal.confidence / 100.0)
        risk_amount = base_risk * conf_factor

        position_size = risk_amount / sl_distance_pct
        # Cap: never risk more than max_risk_pct% of account
        max_position  = self._account_size * self._max_risk_pct / 100.0 / sl_distance_pct
        return round(min(position_size, max_position), 2)

    def _is_correlated_open(
        self, symbol: str, open_symbols: Set[str]
    ) -> bool:
        """Return True if a correlated pair is already in an open trade."""
        for group in self._corr_groups:
            if symbol in group:
                for other in group:
                    if other != symbol and other in open_symbols:
                        return True
        return False

    def _next_day_start_ts(self) -> float:
        """Epoch timestamp for midnight IST tomorrow."""
        now = datetime.now(IST)
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
        import datetime as dt_mod
        tomorrow = tomorrow + dt_mod.timedelta(days=1)
        return tomorrow.timestamp()

    async def _maybe_reset_daily_pnl(self) -> None:
        """Reset daily PnL counter at IST midnight or on startup."""
        now_ts = time.time()
        # Initial startup or day rollover
        if self._daily_reset_ts == 0 or now_ts >= self._daily_reset_ts:
            # Calculate IST day start for current day
            now_ist = datetime.now(IST)
            day_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
            day_start_ts = int(day_start_ist.timestamp() * 1000)
            day_end_ts = int((day_start_ist.timestamp() + 86400) * 1000)
            
            db_pnl = await self._db.get_daily_pnl(day_start_ts, day_end_ts)
            self._daily_pnl = db_pnl
            self._daily_reset_ts = self._next_day_start_ts()
            logger.info("Daily PnL synced from DB: %.4f USDT. Next reset at IST midnight.", db_pnl)

    # ─────────────────────────────────────────────────────────────────────
    # Status
    # ─────────────────────────────────────────────────────────────────────

    def status_dict(self) -> Dict[str, Any]:
        return {
            "paused":          self._paused,
            "daily_pnl":       round(self._daily_pnl, 4),
            "max_daily_loss":  round(self._account_size * self._max_daily_loss_p / 100, 2),
            "account_size":    self._account_size,
            "within_hours":    self._within_trading_hours(),
        }
