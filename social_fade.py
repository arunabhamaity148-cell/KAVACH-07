"""
KAVACH-07 Strategy: Social Fade
Contrarian strategy fading extreme social sentiment and Fear & Greed extremes.
Extreme greed (F&G > 80) → SHORT. Extreme fear (F&G < 20) → LONG.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import Signal, StrategyBase
from ..core.indicators import sl_long, sl_short, tp_long, tp_short

logger = logging.getLogger(__name__)


class SocialFade(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        greed_thresh = int(self._cfg.get("fng_extreme_greed_threshold", 80))
        fear_thresh  = int(self._cfg.get("fng_extreme_fear_threshold", 20))
        sl_pct       = float(self._cfg.get("sl_percent", 1.5))
        tp_pct       = float(self._cfg.get("tp_percent", 3.0))

        fng   = md.fng_index   # 0–100: 0=Extreme Fear, 100=Extreme Greed
        price = md.price

        if fng == 50:
            return self._neutral("F&G Index at neutral (50) or unavailable")

        try:
            if fng >= greed_thresh:
                # Extreme greed → market over-extended → contrarian SHORT
                extremity = (fng - greed_thresh) / (100 - greed_thresh + 1e-5)
                confidence = min(75.0, 40.0 + extremity * 30.0)
                rationale = (
                    f"EXTREME GREED: F&G={fng} (threshold={greed_thresh}) "
                    f"→ Contrarian SHORT (crowd too bullish, reversal risk)"
                )
                return self._create_signal(
                    self.symbol, "SHORT", confidence, price,
                    sl_short(price, sl_pct), tp_short(price, tp_pct), rationale
                )

            elif fng <= fear_thresh:
                # Extreme fear → market over-sold → contrarian LONG
                extremity = (fear_thresh - fng) / (fear_thresh + 1e-5)
                confidence = min(75.0, 40.0 + extremity * 30.0)
                rationale = (
                    f"EXTREME FEAR: F&G={fng} (threshold={fear_thresh}) "
                    f"→ Contrarian LONG (crowd too bearish, bounce risk)"
                )
                return self._create_signal(
                    self.symbol, "LONG", confidence, price,
                    sl_long(price, sl_pct), tp_long(price, tp_pct), rationale
                )

            return self._neutral(
                f"F&G={fng} within neutral range [{fear_thresh}, {greed_thresh}]"
            )

        except Exception as exc:
            logger.error("SocialFade[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
