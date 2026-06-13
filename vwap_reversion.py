"""
KAVACH-07 Strategy: VWAP Reversion
Trade mean reversion when price deviates significantly from intraday VWAP.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import Signal, StrategyBase
from ..core.indicators import sl_long, sl_short

logger = logging.getLogger(__name__)


class VwapReversion(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        dev_thresh = float(self._cfg.get("vwap_deviation_threshold_percent", 0.8)) / 100.0
        sl_pct     = float(self._cfg.get("sl_percent", 1.2))

        price = md.price
        vwap  = md.vwap

        if vwap <= 0:
            return self._neutral("VWAP not yet calculated")

        try:
            deviation = (price - vwap) / vwap  # + = above, - = below

            if abs(deviation) < dev_thresh:
                return self._neutral(
                    f"Price {price:.4f} within {dev_thresh*100:.2f}% of VWAP {vwap:.4f} "
                    f"(dev={deviation*100:.2f}%)"
                )

            deviation_multiple = abs(deviation) / dev_thresh
            confidence = min(88.0, 48.0 + (deviation_multiple - 1.0) * 15.0)

            if deviation < 0:
                # Price significantly BELOW VWAP → mean reversion LONG (target = VWAP)
                potential_gain = abs(deviation) * 100.0
                rationale = (
                    f"Price {deviation*100:.2f}% BELOW VWAP {vwap:.4f} "
                    f"({deviation_multiple:.1f}x threshold) → Mean reversion LONG "
                    f"(TP at VWAP, +{potential_gain:.2f}% potential)"
                )
                # TP is VWAP itself (reversion target)
                tp = round(vwap, 8)
                sl = sl_long(price, sl_pct)
                return self._create_signal(
                    self.symbol, "LONG", confidence, price, sl, tp, rationale
                )

            else:
                # Price significantly ABOVE VWAP → mean reversion SHORT (target = VWAP)
                potential_gain = deviation * 100.0
                rationale = (
                    f"Price +{deviation*100:.2f}% ABOVE VWAP {vwap:.4f} "
                    f"({deviation_multiple:.1f}x threshold) → Mean reversion SHORT "
                    f"(TP at VWAP, -{potential_gain:.2f}% potential)"
                )
                tp = round(vwap, 8)
                sl = sl_short(price, sl_pct)
                return self._create_signal(
                    self.symbol, "SHORT", confidence, price, sl, tp, rationale
                )

        except Exception as exc:
            logger.error("VwapReversion[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
