"""
KAVACH-07 — DEX-CEX Arbitrage Strategy
Signals directional trades on Hyperliquid by exploiting funding rate 
differentials relative to Binance Futures.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.dex_cex_arb")

class DexCexArb(StrategyBase):
    """
    Logic:
    1. Retrieve the real-time funding rates for the asset on 
       Hyperliquid (DEX) and Binance (CEX).
    2. Calculate the Spread: Diff = HL_Funding - Binance_Funding.
    3. Threshold: Trigger if abs(Diff) >= funding_diff_threshold (default 0.03%).
    4. Directional Bias (Arb Leg):
       - If HL_Funding > Binance_Funding + 0.03%: 
         Hyperliquid longs are overpaying relative to Binance. 
         Signal: SHORT on Hyperliquid (to capture premium/mean-reversion).
       - If HL_Funding < Binance_Funding - 0.03%: 
         Hyperliquid shorts are overpaying relative to Binance. 
         Signal: LONG on Hyperliquid.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        # funding_diff_threshold: 0.0003 (0.03%)
        threshold = float(self._cfg.get("funding_diff_threshold", 0.0003))
        sl_pct = float(self._cfg.get("sl_percent", 0.3)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 0.6)) / 100.0

        # Retrieve funding rates
        # hl_funding: Hyperliquid rate
        # funding_rate: Binance rate (standardized in DataEngine)
        hl_rate = md.hl_funding
        bnb_rate = md.funding_rate
        current_price = md.price

        try:
            # Calculate the differential
            # Positive diff means HL is more expensive for longs
            diff = hl_rate - bnb_rate
            abs_diff = abs(diff)

            if abs_diff < threshold:
                return self._neutral(
                    f"Funding spread {diff*100:.4f}% below threshold {threshold*100:.3f}%"
                )

            # Determine Side
            if diff > 0:
                # HL > BNB: capturing the premium by going SHORT on HL
                side = "SHORT"
                rationale = (
                    f"DEX-CEX Arb: Hyperliquid funding ({hl_rate*100:.4f}%) is significantly "
                    f"HIGHER than Binance ({bnb_rate*100:.4f}%). "
                    f"Spread: {diff*100:.4f}%. Signaling SHORT on HL."
                )
            else:
                # HL < BNB: capturing the premium by going LONG on HL
                side = "LONG"
                rationale = (
                    f"DEX-CEX Arb: Hyperliquid funding ({hl_rate*100:.4f}%) is significantly "
                    f"LOWER than Binance ({bnb_rate*100:.4f}%). "
                    f"Spread: {abs_diff*100:.4f}%. Signaling LONG on HL."
                )

            # Confidence scales with spread magnitude
            # 0.03% spread -> 65% Conf, 0.10% spread -> 90% Conf
            excess = abs_diff - threshold
            conf = 65.0 + (excess * 10000.0 * 3.5) # ~3.5% conf per 0.01% excess spread
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
                    "hl_funding": round(hl_rate, 8),
                    "bnb_funding": round(bnb_rate, 8),
                    "spread": round(diff, 8)
                }
            )

        except Exception as e:
            logger.error("DexCexArb error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")