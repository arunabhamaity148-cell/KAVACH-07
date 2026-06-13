"""
KAVACH-07 — Funding Squeeze Strategy
Signals contrarian reversals when funding rates reach extreme levels, 
indicating over-leveraged positioning susceptible to a squeeze.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.funding_squeeze")

class FundingSqueeze(StrategyBase):
    """
    Logic:
    1. Monitor the real-time funding rate from Binance Futures.
    2. If funding > extreme_positive_threshold:
       - Longs are paying shorts significantly.
       - High probability of a 'Long Squeeze'.
       - Signal: SHORT.
    3. If funding < extreme_negative_threshold:
       - Shorts are paying longs significantly.
       - High probability of a 'Short Squeeze'.
       - Signal: LONG.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        pos_thresh = float(self._cfg.get("extreme_positive_threshold", 0.0005)) # 0.05%
        neg_thresh = float(self._cfg.get("extreme_negative_threshold", -0.0005)) # -0.05%
        sl_pct = float(self._cfg.get("sl_percent", 0.5)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 1.0)) / 100.0

        funding_rate = md.funding_rate
        current_price = md.price

        # Requirement: Zero funding is a valid state, not 'unavailable'. 
        # But for this strategy, we only trigger on extremes.
        
        is_extreme_positive = funding_rate >= pos_thresh
        is_extreme_negative = funding_rate <= neg_thresh

        if not (is_extreme_positive or is_extreme_negative):
            return self._neutral(f"Funding rate {funding_rate:.6f} within normal bounds")

        try:
            # Determine Direction (Contrarian)
            if is_extreme_positive:
                side = "SHORT"
                rationale = (
                    f"Extreme POSITIVE funding detected ({funding_rate*100:.4f}%). "
                    f"Longs over-leveraged. Anticipating Long Squeeze reversal."
                )
            else:
                side = "LONG"
                rationale = (
                    f"Extreme NEGATIVE funding detected ({funding_rate*100:.4f}%). "
                    f"Shorts over-leveraged. Anticipating Short Squeeze reversal."
                )

            # Confidence scales with the extremity of the funding rate
            # Base 60% + 5% for every 0.01% beyond threshold, capped at 90%
            excess = abs(funding_rate) - abs(pos_thresh if is_extreme_positive else neg_thresh)
            conf = 60.0 + (excess * 10000.0 * 5.0) 
            conf = max(60.0, min(90.0, conf))

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
                    "funding_rate": round(funding_rate, 6),
                    "threshold_breached": round(pos_thresh if is_extreme_positive else neg_thresh, 6)
                }
            )

        except Exception as e:
            logger.error("FundingSqueeze error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")