"""
KAVACH-07 Strategy: Funding Squeeze
Contrarian signal on extreme positive/negative funding rates.
Extreme negative → potential short squeeze → LONG.
Extreme positive → potential long squeeze → SHORT.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import Signal, StrategyBase
from ..core.indicators import sl_long, sl_short, tp_long, tp_short

logger = logging.getLogger(__name__)


class FundingSqueeze(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        neg_thresh = float(self._cfg.get("extreme_negative_threshold", -0.0005))
        pos_thresh = float(self._cfg.get("extreme_positive_threshold", 0.0005))
        sl_pct     = float(self._cfg.get("sl_percent", 1.2))
        tp_pct     = float(self._cfg.get("tp_percent", 2.0))

        fr = md.funding_rate
        price = md.price

        if fr == 0.0:
            return self._neutral("Funding rate unavailable")

        try:
            if fr <= neg_thresh:
                # Extreme negative → shorts are heavily paying → potential short squeeze
                extremity = abs(fr / neg_thresh)  # > 1.0 means beyond threshold
                confidence = min(92.0, 50.0 + (extremity - 1.0) * 30.0)
                rationale = (
                    f"EXTREME NEGATIVE funding: {fr*100:.4f}% (threshold {neg_thresh*100:.4f}%) "
                    f"→ Short squeeze risk. Contrarian LONG."
                )
                return self._create_signal(
                    self.symbol, "LONG", confidence, price,
                    sl_long(price, sl_pct), tp_long(price, tp_pct), rationale
                )

            elif fr >= pos_thresh:
                # Extreme positive → longs are heavily paying → potential long squeeze
                extremity = fr / pos_thresh
                confidence = min(92.0, 50.0 + (extremity - 1.0) * 30.0)
                rationale = (
                    f"EXTREME POSITIVE funding: {fr*100:.4f}% (threshold {pos_thresh*100:.4f}%) "
                    f"→ Long squeeze risk. Contrarian SHORT."
                )
                return self._create_signal(
                    self.symbol, "SHORT", confidence, price,
                    sl_short(price, sl_pct), tp_short(price, tp_pct), rationale
                )

            return self._neutral(
                f"Funding rate {fr*100:.4f}% within normal range "
                f"[{neg_thresh*100:.4f}%, {pos_thresh*100:.4f}%]"
            )

        except Exception as exc:
            logger.error("FundingSqueeze[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
