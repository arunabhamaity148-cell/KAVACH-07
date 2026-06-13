"""
KAVACH-07 — VWAP Reversion Strategy
Signals mean reversion trades when price deviates significantly from the intraday VWAP.
TP is set as the VWAP price itself, rather than a fixed percentage.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.vwap_reversion")

class VwapReversion(StrategyBase):
    """
    Logic:
    1. Retrieve the real-time VWAP and current price from MarketData.
    2. Calculate the percentage deviation: (Price - VWAP) / VWAP.
    3. If Deviation > Threshold (0.8%):
       - LONG if price is below VWAP.
       - SHORT if price is above VWAP.
    4. TP is strictly the VWAP price. SL is derived from sl_percent.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        threshold = float(self._cfg.get("deviation_threshold", 0.008))
        sl_pct = float(self._cfg.get("sl_percent", 1.0)) / 100.0
        
        # Current Stats
        current_price = md.price
        # VWAP calculation from DataEngine: md.vwap_num / md.vwap_den
        if md.vwap_den <= 0:
            return self._neutral("VWAP denominator is zero")
            
        vwap = md.vwap_num / md.vwap_den
        
        try:
            # Calculate Deviation
            # Formula: (Price - VWAP) / VWAP
            deviation = (current_price - vwap) / vwap
            abs_dev = abs(deviation)

            if abs_dev < threshold:
                return self._neutral(f"Price within VWAP threshold (Dev: {deviation*100:.3f}%)")

            # Determine Side (Contrarian Reversion)
            if deviation > 0:
                # Price is significantly ABOVE VWAP -> SHORT back to VWAP
                side = "SHORT"
                rationale = (
                    f"VWAP Reversion: Price is {deviation*100:.2f}% ABOVE intraday VWAP (${vwap:.6g}). "
                    f"Anticipating mean reversion to the downside."
                )
                tp = vwap # TP is exactly the VWAP price
                sl = current_price * (1.0 + sl_pct)
            else:
                # Price is significantly BELOW VWAP -> LONG back to VWAP
                side = "LONG"
                rationale = (
                    f"VWAP Reversion: Price is {abs(deviation)*100:.2f}% BELOW intraday VWAP (${vwap:.6g}). "
                    f"Anticipating mean reversion to the upside."
                )
                tp = vwap
                sl = current_price * (1.0 - sl_pct)

            # Confidence scales with the magnitude of deviation
            # 0.8% -> 60% Conf, 2.0% -> 90% Conf
            conf = 60.0 + (abs_dev - threshold) * 2500.0
            conf = max(60.0, min(95.0, conf))

            return self._create_signal(
                side=side,
                confidence=conf,
                entry=current_price,
                stop_loss=sl,
                take_profit=tp,
                rationale=rationale,
                extra_metadata={
                    "vwap": round(vwap, 6),
                    "deviation_pct": round(deviation * 100, 4)
                }
            )

        except Exception as e:
            logger.error("VwapReversion error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")