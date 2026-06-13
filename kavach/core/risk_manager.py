"""
KAVACH-07 — Risk Manager
Orchestrates all safety checks, position sizing, and regulatory filters.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

import numpy as np
import pytz

logger = logging.getLogger("kavach.risk_manager")

class RiskManager:
    """
    Maintains trading state and enforces nuclear-grade risk controls.
    """

    def __init__(self, config: dict, db_manager: Any):
        self._cfg = config
        self._db = db_manager
        
        # Risk Constants
        self._account_size = float(config["account"]["size_usdt"])
        self._max_risk_pct = float(config["account"]["max_risk_per_trade_percent"]) / 100.0
        self._daily_loss_limit = float(config["account"]["max_daily_loss_percent"]) / 100.0
        self._max_open_trades = int(config["account"]["max_open_trades"])
        self._notional_cap_pct = float(config["account"]["max_notional_per_trade_percent"]) / 100.0
        
        # Filtering Constants
        self._ist = pytz.timezone(config["bot"]["timezone"])
        self._start_hour = int(config["trading"]["start_hour_ist"])
        self._end_hour = int(config["trading"]["end_hour_ist"])
        self._corr_threshold = float(config["risk"]["correlation_threshold"])
        
        # Regulatory FUD normalization
        self._fud_pairs = self._normalize_fud_list(config["risk"]["regulatory_fud_pairs"])
        
        # Whale Multipliers
        self._whale_aligned = float(config["phase2"]["whale"]["whale_aligned_size_multiplier"])
        self._whale_opposed = float(config["phase2"]["whale"]["whale_opposed_size_multiplier"])
        
        # Internal State
        self._daily_pnl: float = 0.0
        self._is_paused: bool = False
        self._pause_until: float = 0.0
        self._news_engine: Optional[Any] = None
        self._whale_engine: Optional[Any] = None
        
        self._lock = asyncio.Lock()

    def set_engines(self, news: Any, whale: Any) -> None:
        """Injects Phase 2 engines."""
        self._news_engine = news
        self._whale_engine = whale

    async def recover_state(self) -> None:
        """Queries DB to sync daily PnL on startup."""
        try:
            self._daily_pnl = await self._db.get_daily_pnl()
            logger.info("Risk Manager: Daily PnL recovered: $%.2f", self._daily_pnl)
        except Exception as e:
            logger.error("Failed to recover daily PnL: %s", e)
            self._daily_pnl = 0.0

    @property
    def is_paused(self) -> bool:
        """Checks if bot is manually or automatically paused."""
        if self._is_paused:
            return True
        if time.time() < self._pause_until:
            return True
        return False

    def resume(self) -> None:
        """Manual resume."""
        self._is_paused = False
        self._pause_until = 0.0
        logger.info("Bot manually RESUMED")

    def pause(self, minutes: int = 0) -> None:
        """Manual or automated pause."""
        if minutes > 0:
            self._pause_until = time.time() + (minutes * 60)
            logger.warning("Bot PAUSED for %d minutes", minutes)
        else:
            self._is_paused = True
            logger.warning("Bot manually PAUSED")

    # ──────────────────────────────────────────────────────────────────────────
    # Filtering Logic
    # ──────────────────────────────────────────────────────────────────────────

    async def filter_signal(self, signal: Any, data_context: Dict[str, Any]) -> Optional[Any]:
        """
        Applies sequential risk gates. 
        Returns enriched signal with position size or None if rejected.
        """
        async with self._lock:
            # 1. Bot State
            if self.is_paused:
                return None

            # 2. Daily Loss Check
            if self._daily_pnl <= -(self._account_size * self._daily_loss_limit):
                logger.critical("NUCLEAR ALERT: Daily loss limit hit ($%.2f). Pausing bot.", self._daily_pnl)
                self.pause()
                return None

            # 3. Trading Hours (IST)
            if not self._is_within_hours():
                return None

            # 4. Regulatory FUD
            if self._is_fud_pair(signal.symbol):
                logger.warning("Signal rejected: Regulatory FUD pair %s", signal.symbol)
                return None

            # 5. Max Open Trades
            open_trades = await self._db.get_open_trades()
            if len(open_trades) >= self._max_open_trades:
                return None

            # 6. Correlation Check
            if not self._check_correlation(signal.symbol, open_trades, data_context):
                logger.warning("Signal rejected: High correlation with open positions (%s)", signal.symbol)
                return None

            # 7. News Impact Block (Phase 2)
            if self._news_engine:
                news_status = self._news_engine.get_status()
                if news_status["score"] <= -7.0 and news_status["impact"] == "HIGH":
                    logger.critical("EMERGENCY: Negative news impact detected. Pausing 60 min.")
                    self.pause(60)
                    return None

            # 8. Position Sizing
            pos_size = self._calculate_position_size(signal)
            if pos_size <= 0:
                return None

            signal.position_size_usdt = pos_size
            return signal

    # ──────────────────────────────────────────────────────────────────────────
    # Internal Math
    # ──────────────────────────────────────────────────────────────────────────

    def _calculate_position_size(self, signal: Any) -> float:
        """
        Position Size = (Account * Risk% * Confidence) / SL_Distance
        Formula re-derivation:
        Target Loss = Account * Risk% * Confidence_Factor
        SL_Distance_Pct = (Entry - SL) / Entry
        Size = Target Loss / SL_Distance_Pct
        """
        sl_dist = abs(signal.entry - signal.stop_loss) / signal.entry
        if sl_dist < 0.0005: # Minimum 0.05% SL distance to prevent infinity
            logger.error("Risk: SL distance too tight for %s (%.4f)", signal.symbol, sl_dist)
            return 0.0

        confidence_factor = signal.confidence / 100.0
        target_loss = self._account_size * self._max_risk_pct * confidence_factor
        
        base_size = target_loss / sl_dist
        
        # Whale Bias Adjustment
        if self._whale_engine:
            bias = self._whale_engine.get_bias()
            if bias != "NEUTRAL":
                is_aligned = (bias == "BULLISH" and signal.side == "LONG") or \
                             (bias == "BEARISH" and signal.side == "SHORT")
                base_size *= self._whale_aligned if is_aligned else self._whale_opposed

        # Notional Cap (20% of account)
        max_notional = self._account_size * self._notional_cap_pct
        final_size = min(base_size, max_notional)
        
        return round(final_size, 2)

    def _is_within_hours(self) -> bool:
        """Validates trading window 09:00 - 00:00 IST."""
        now = datetime.now(self._ist)
        # Handle midnight rollover (00:00)
        current_minute = now.hour * 60 + now.minute
        start_minute = self._start_hour * 60
        # If end_hour is 0, it means 24:00
        end_minute = 24 * 60 if self._end_hour == 0 else self._end_hour * 60
        
        return start_minute <= current_minute < end_minute

    def _is_fud_pair(self, symbol: str) -> bool:
        """Suffix-agnostic FUD check."""
        # Convert BTCUSDT -> BTC
        clean_sym = symbol.replace("USDT", "").replace("BUSD", "")
        return clean_sym in self._fud_pairs

    def _check_correlation(self, symbol: str, open_trades: list, data_ctx: Dict[str, Any]) -> bool:
        """Calculates Pearson correlation between proposed and open symbols."""
        if not open_trades:
            return True

        target_md = data_ctx.get(symbol)
        if not target_md or len(target_md.klines_1m) < 60:
            return True

        # Extract 1h closes for target
        target_closes = np.array([k[4] for k in list(target_md.klines_1m)[-60:]])
        
        for trade in open_trades:
            open_md = data_ctx.get(trade["symbol"])
            if not open_md or len(open_md.klines_1m) < 60:
                continue
                
            open_closes = np.array([k[4] for k in list(open_md.klines_1m)[-60:]])
            
            # Pearson r
            matrix = np.corrcoef(target_closes, open_closes)
            correlation = matrix[0, 1]
            
            if correlation > self._corr_threshold:
                return False
                
        return True

    def _normalize_fud_list(self, raw_list: List[str]) -> Set[str]:
        """Cleanses config FUD list into base symbols."""
        return {s.replace("USDT", "").replace("BUSD", "") for s in raw_list}

    def _normalize_symbol(self, symbol: str) -> str:
        """Specific fix for BEAMGUSDT -> BEAM."""
        if "BEAMG" in symbol:
            return "BEAM"
        return symbol.replace("USDT", "").replace("BUSD", "")