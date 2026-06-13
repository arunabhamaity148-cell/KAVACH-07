"""
KAVACH-07 — Market Maker PnL Strategy
Infers the unrealized PnL of dominant market participants (Market Makers/OI holders)
to signal potential "pain points" where forced de-risking or squeezes occur.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np
from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.market_maker_pnl")

class MarketMakerPnl(StrategyBase):
    """
    Logic:
    1. Estimate the "Average Entry Price" of the current Open Interest.
    2. We use a volume-weighted average price (VWAP) over a specific lookback
       as a proxy for the collective entry of active positions.
    3. Calculate MM PnL % = (Current Price - Estimated Entry) / Estimated Entry.
    4. Threshold: If |PnL %| >= pnl_divergence_threshold (default 15%).
    5. Directional Bias:
       - If CVD is positive (Net Long) and Price is significantly BELOW Entry:
         Longs are in pain -> Potential capitulation -> SHORT.
       - If CVD is negative (Net Short) and Price is significantly ABOVE Entry:
         Shorts are in pain -> Potential squeeze -> LONG.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        # Threshold: 15.0 (%)
        threshold_pct = float(self._cfg.get("pnl_divergence_threshold", 15.0))
        sl_pct = float(self._cfg.get("sl_percent", 1.0)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 2.0)) / 100.0

        try:
            # 1. Estimate Collective Entry Price
            # We use the 5m kline history (approx 4 hours) to estimate where 
            # the bulk of current OI was opened.
            if len(md.klines_5m) < 48: # 4 hours of 5m bars
                return self._neutral("Insufficient history for entry estimation")

            klines = np.array(list(md.klines_5m))
            # VWAP over the last 48 bars
            prices = klines[-48:, 4] # Closes
            vols = klines[-48:, 5]   # Volumes
            
            est_entry_price = np.average(prices, weights=vols)
            current_price = md.price
            
            if est_entry_price <= 0:
                return self._neutral("Invalid estimated entry price")

            # 2. Calculate Unrealized PnL Divergence
            # This represents the "Pain/Gain" of the average participant
            pnl_divergence = (current_price - est_entry_price) / est_entry_price
            pnl_div_pct = pnl_divergence * 100.0

            # 3. Determine Positioning Bias via CVD
            # Positive CVD implies net long aggression, Negative implies net short
            is_net_long = md.cvd > 0
            is_net_short = md.cvd < 0

            # 4. Signal Logic (Forced de-risking points)
            side = "NEUTRAL"
            rationale = ""

            # Case A: Net Longs are in deep loss (Price << Entry)
            if is_net_long and pnl_div_pct <= -threshold_pct:
                side = "SHORT"
                rationale = (
                    f"MM PnL: Estimated Long Entry ${est_entry_price:.6g}. "
                    f"Current price is {abs(pnl_div_pct):.1f}% BELOW entry. "
                    f"CVD is positive (Net Long). Anticipating forced Long capitulation."
                )

            # Case B: Net Shorts are in deep loss (Price >> Entry)
            elif is_net_short and pnl_div_pct >= threshold_pct:
                side = "LONG"
                rationale = (
                    f"MM PnL: Estimated Short Entry ${est_entry_price:.6g}. "
                    f"Current price is {pnl_div_pct:.1f}% ABOVE entry. "
                    f"CVD is negative (Net Short). Anticipating forced Short squeeze."
                )

            if side == "NEUTRAL":
                return self._neutral(f"PnL Divergence ({pnl_div_pct:.1f}%) below threshold")

            # 5. Confidence Calculation
            # 15% Div -> 65% Conf, 30% Div -> 90% Conf
            excess_pain = abs(pnl_div_pct) - threshold_pct
            conf = 65.0 + (excess_pain * 1.5)
            conf = max(65.0, min(95.0, conf))

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
                    "est_entry_price": round(est_entry_price, 6),
                    "pnl_divergence_pct": round(pnl_div_pct, 2),
                    "participant_bias": "LONG" if is_net_long else "SHORT"
                }
            )

        except Exception as e:
            logger.error("MarketMakerPnl error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")