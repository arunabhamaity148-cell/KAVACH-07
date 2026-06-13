"""
KAVACH-07 — Post-Settlement Reversion Strategy
Signals directional trades based on mean-reversion following high-volatility 
settlement/funding windows (00:00, 08:00, 16:00 UTC).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

import numpy as np
from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.post_settlement")

class PostSettlement(StrategyBase):
    """
    Logic:
    1. Settlement in crypto futures often creates "price distortion" due to 
       forced liquidations or heavy funding-induced positioning.
    2. Monitor the price extension relative to the price N hours ago 
       (default 4h) leading into the settlement window.
    3. If the price has deviated significantly (target_percent), 
       anticipate a mean-reversion move.
    4. Trigger: Reversion signal is generated shortly after the 8h 
       funding/settlement intervals.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        lookback_h = int(self._cfg.get("lookback_hours", 4))
        target_reversion = float(self._cfg.get("target_percent", 0.015)) # 1.5%
        sl_pct = float(self._cfg.get("sl_percent", 1.0)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 1.5)) / 100.0

        try:
            # 1. Timing Check (UTC Windows)
            # Funding/Settlement typically occurs at 00:00, 08:00, 16:00 UTC.
            now_utc = datetime.now(timezone.utc)
            current_hour = now_utc.hour
            current_minute = now_utc.minute
            
            # Strategy is active only within 60 minutes after a 8h window
            windows = [0, 8, 16]
            is_post_window = any(current_hour == w and current_minute <= 59 for w in windows)
            
            if not is_post_window:
                return self._neutral(f"Outside settlement window (Current UTC: {current_hour:02d}:{current_minute:02d})")

            # 2. Reversion Math
            # extension = (Current Price / Price 4h ago) - 1
            # We need 240 1m klines for 4h
            klines_needed = lookback_h * 60
            if len(md.klines_1m) < klines_needed:
                return self._neutral(f"Insufficient 1m history for {lookback_h}h baseline")

            klines_list = list(md.klines_1m)
            baseline_price = klines_list[-klines_needed][4] # Close of the baseline candle
            current_price = md.price

            if baseline_price <= 0:
                return self._neutral("Invalid baseline price")

            extension = (current_price / baseline_price) - 1.0
            abs_ext = abs(extension)

            if abs_ext < target_reversion:
                return self._neutral(
                    f"Price extension ({extension*100:.2f}%) below reversion target ({target_reversion*100:.2f}%)"
                )

            # 3. Determine Side (Contrarian)
            # If price pumped > 1.5% into settlement -> SHORT
            # If price dumped > 1.5% into settlement -> LONG
            if extension > 0:
                side = "SHORT"
                rationale = (
                    f"Post-Settlement Reversion: Price extended {extension*100:.2f}% UP "
                    f"into UTC {current_hour:02d}:00 window. Anticipating mean-reversion pullback."
                )
            else:
                side = "LONG"
                rationale = (
                    f"Post-Settlement Reversion: Price extended {abs(extension)*100:.2f}% DOWN "
                    f"into UTC {current_hour:02d}:00 window. Anticipating mean-reversion bounce."
                )

            # 4. Confidence Calculation
            # 1.5% extension -> 65% Conf, 4% extension -> 90% Conf
            conf = 65.0 + (abs_ext - target_reversion) * 1000.0
            conf = max(65.0, min(92.0, conf))

            entry = current_price
            if side == "LONG":
                sl = entry * (1.0 - sl_pct)
                tp = entry * (1.0 + tp_pct)
            else:
                sl = entry * (1.0 + sl_pct)
                tp = entry * (1.0 - tp_pct)

            return self._create_signal(
                side=side,
                confidence=conf,
                entry=entry,
                stop_loss=sl,
                take_profit=tp,
                rationale=rationale,
                extra_metadata={
                    "settlement_window_utc": f"{current_hour:02d}:00",
                    "extension_pct": round(extension * 100, 4),
                    "baseline_price": baseline_price
                }
            )

        except Exception as e:
            logger.error("PostSettlement error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")