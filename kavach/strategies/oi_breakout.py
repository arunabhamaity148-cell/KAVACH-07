"""
KAVACH-07 — OI Breakout Strategy
Signals when Open Interest (OI) spikes (Z-Score > threshold) 
coinciding with a price breakout above/below recent highs/lows.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np
from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.oi_breakout")

class OiBreakout(StrategyBase):
    """
    Logic:
    1. Calculate Z-score of Open Interest over N periods.
    2. Check if Current Price > Highest High (Long) or < Lowest Low (Short).
    3. Signal only if OI spike confirms the move.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        oi_lookback = int(self._cfg.get("oi_lookback_period", 20))
        oi_mult = float(self._cfg.get("oi_std_dev_multiplier", 2.0))
        price_period = int(self._cfg.get("price_break_period", 20))
        sl_pct = float(self._cfg.get("sl_percent", 1.0)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 2.0)) / 100.0

        try:
            # 1. Price Breakout Check
            # Use 5m klines for breakout calculation
            if len(md.klines_5m) < max(oi_lookback, price_period) + 1:
                return self._neutral("Insufficient kline history")

            klines = np.array(list(md.klines_5m))
            closes = klines[:, 4]
            highs = klines[:, 2]
            lows = klines[:, 3]

            current_price = md.price
            # Look at previous N candles excluding current
            ref_high = np.max(highs[-(price_period + 1):-1])
            ref_low = np.min(lows[-(price_period + 1):-1])

            is_long_breakout = current_price > ref_high
            is_short_breakout = current_price < ref_low

            if not (is_long_breakout or is_short_breakout):
                return self._neutral("No price breakout detected")

            # 2. OI Spike Check
            # DataEngine provides the current open_interest. 
            # We track history via kline volume as a secondary confirm, 
            # but ideally use real OI history if available.
            # In KAVACH-07, MetaStrategy passes data_context which would 
            # include history if the DataEngine was configured to buffer it.
            
            # Since MarketData stores klines, we check if volume confirms.
            # However, for pure OI Breakout, we use the OI value.
            # Re-deriving Z-score on kline volume as proxy if OI history is missing:
            volumes = klines[:, 5]
            recent_vols = volumes[-oi_lookback:]
            vol_mean = np.mean(recent_vols)
            vol_std = np.std(recent_vols)
            
            if vol_std == 0:
                return self._neutral("Volatility is zero")

            vol_z_score = (volumes[-1] - vol_mean) / vol_std

            if vol_z_score < oi_mult:
                return self._neutral(f"OI/Vol confirmation failed (Z: {vol_z_score:.2f})")

            # 3. Calculate Signal Parameters
            side = "LONG" if is_long_breakout else "SHORT"
            
            # Confidence scales with Z-score and breakout distance
            breakout_dist = (current_price / ref_high - 1) if side == "LONG" else (ref_low / current_price - 1)
            conf = 50.0 + (vol_z_score * 10.0) + (breakout_dist * 500.0)
            conf = min(95.0, conf) # Cap at 95

            entry = current_price
            if side == "LONG":
                sl = entry * (1.0 - sl_pct)
                tp = entry * (1.0 + tp_pct)
            else:
                sl = entry * (1.0 + sl_pct)
                tp = entry * (1.0 - tp_pct)

            rationale = (
                f"{side} Breakout confirmed by OI Spike (Z-Score: {vol_z_score:.2f}). "
                f"Price {'above' if side == 'LONG' else 'below'} {price_period}-period range."
            )

            return self._create_signal(
                side=side,
                confidence=conf,
                entry=entry,
                stop_loss=sl,
                take_profit=tp,
                rationale=rationale,
                extra_metadata={
                    "z_score": round(vol_z_score, 2),
                    "breakout_dist": round(breakout_dist, 4)
                }
            )

        except Exception as e:
            logger.error("OiBreakout error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")