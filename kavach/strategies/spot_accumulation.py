"""
KAVACH-07 — Spot Accumulation Strategy
Detects institutional accumulation or distribution by identifying volume spikes 
relative to a rolling baseline, with direction determined by price action.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np
from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.spot_accumulation")

class SpotAccumulation(StrategyBase):
    """
    Logic:
    1. Calculate average volume over the last N periods (default 20).
    2. Check if the current period's volume is >= 2.5x the average.
    3. Determine direction:
       - If Close > Open during spike: Accumulation -> LONG.
       - If Close < Open during spike: Distribution -> SHORT.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        vol_mult = float(self._cfg.get("volume_spike_multiplier", 2.5))
        lookback = int(self._cfg.get("lookback_period", 20))
        sl_pct = float(self._cfg.get("sl_percent", 1.0)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 2.0)) / 100.0

        try:
            if len(md.klines_5m) < lookback + 1:
                return self._neutral("Insufficient history for volume baseline")

            # Extract volume and price data
            klines = np.array(list(md.klines_5m))
            # [time, o, h, l, c, v]
            volumes = klines[:, 5]
            
            # Baseline: Mean volume of previous N candles
            avg_vol = np.mean(volumes[-(lookback + 1):-1])
            if avg_vol <= 0:
                return self._neutral("Average volume is zero")

            current_vol = volumes[-1]
            vol_ratio = current_vol / avg_vol

            # 1. Volume Spike Check
            if vol_ratio < vol_mult:
                return self._neutral(f"Volume ratio ({vol_ratio:.2f}x) below threshold ({vol_mult}x)")

            # 2. Determine Direction
            c_open = klines[-1, 1]
            c_close = klines[-1, 4]
            
            if c_close > c_open:
                side = "LONG"
                rationale = (
                    f"Spot Accumulation: Institutional buying detected. "
                    f"Volume spike {vol_ratio:.1f}x above average on bullish candle."
                )
            elif c_close < c_open:
                side = "SHORT"
                rationale = (
                    f"Spot Distribution: Institutional selling detected. "
                    f"Volume spike {vol_ratio:.1f}x above average on bearish candle."
                )
            else:
                return self._neutral("Neutral price action during volume spike")

            # 3. Confidence Calculation
            # Scales with volume ratio: 2.5x -> 65%, 5.0x -> 85%
            conf = 65.0 + (vol_ratio - vol_mult) * 8.0
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
                    "vol_ratio": round(vol_ratio, 2),
                    "avg_vol": round(avg_vol, 2),
                    "current_vol": round(current_vol, 2)
                }
            )

        except Exception as e:
            logger.error("SpotAccumulation error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")