"""
KAVACH-07 — Social Fade Strategy
Signals contrarian trades based on extreme sentiment readings from the 
Crypto Fear & Greed Index. Fades euphoria and panic.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.social_fade")

class SocialFade(StrategyBase):
    """
    Logic:
    1. Retrieve the latest Fear & Greed Index (0-100).
    2. Thresholds:
       - Greed > threshold (default 80): Excessive euphoria -> SHORT.
       - Fear < threshold (default 20): Excessive panic -> LONG.
    3. Counter-trend approach: Market usually reverses at sentiment extremes.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        greed_thresh = float(self._cfg.get("greed_threshold", 80.0))
        fear_thresh = float(self._cfg.get("fear_threshold", 20.0))
        sl_pct = float(self._cfg.get("sl_percent", 1.0)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 2.0)) / 100.0

        # Retrieve F&G index from data context (Expected from DataEngine poll)
        # As per spec: data field = None if API unavailable
        fng_index = data_context.get("fear_and_greed_index")

        if fng_index is None:
            return self._neutral("Fear & Greed Index data unavailable")

        try:
            is_euphoric = fng_index >= greed_thresh
            is_panicked = fng_index <= fear_thresh

            if not (is_euphoric or is_panicked):
                return self._neutral(f"Sentiment Index ({fng_index}) within neutral bounds")

            # Determine Side (Contrarian Fade)
            if is_euphoric:
                # Extreme Greed -> Expect Pullback -> SHORT
                side = "SHORT"
                rationale = (
                    f"Social Fade: Extreme Euphoria detected (F&G: {fng_index}). "
                    f"Market sentiment is overheated. Anticipating contrarian reversal."
                )
            else:
                # Extreme Fear -> Expect Bounce -> LONG
                side = "LONG"
                rationale = (
                    f"Social Fade: Extreme Panic detected (F&G: {fng_index}). "
                    f"Market sentiment is oversold. Anticipating contrarian bounce."
                )

            # Confidence scales with the extremity of the reading
            # If 80 is thresh, 100 is max. Gap = 20.
            # If 20 is thresh, 0 is max. Gap = 20.
            if is_euphoric:
                excess = fng_index - greed_thresh
                conf = 65.0 + (excess / (100.0 - greed_thresh) * 25.0)
            else:
                excess = fear_thresh - fng_index
                conf = 65.0 + (excess / fear_thresh * 25.0)
            
            conf = max(65.0, min(92.0, conf))

            entry = md.price
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
                    "fear_and_greed_score": fng_index,
                    "sentiment_zone": "EUPHORIA" if is_euphoric else "PANIC"
                }
            )

        except Exception as e:
            logger.error("SocialFade error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")