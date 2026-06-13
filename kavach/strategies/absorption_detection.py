"""
KAVACH-07 — Absorption Detection Strategy
Signals potential reversals when high volume is met with minimal price movement,
indicating that large orders are being "absorbed" by passive liquidity.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np
from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.absorption_detection")

class AbsorptionDetection(StrategyBase):
    """
    Logic:
    1. Monitor volume and price range (High - Low) of the most recent candle.
    2. Threshold 1: Volume must be >= X times the average volume of the lookback period.
    3. Threshold 2: Price move (Range %) must be <= Y percent.
    4. Determine Side:
       - Close near High + Absorption = Sellers absorbing aggressive buyers (Bearish) -> SHORT.
       - Close near Low + Absorption = Buyers absorbing aggressive sellers (Bullish) -> LONG.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        vol_mult = float(self._cfg.get("volume_spike_multiplier", 3.0))
        max_range_pct = float(self._cfg.get("max_price_move_percent", 0.1)) / 100.0
        lookback = int(self._cfg.get("lookback_bars", 20))
        sl_pct = float(self._cfg.get("sl_percent", 1.0)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 2.0)) / 100.0

        try:
            if len(md.klines_5m) < lookback + 1:
                return self._neutral("Insufficient history for volume baseline")

            klines = np.array(list(md.klines_5m))
            # [time, o, h, l, c, v]
            last_candle = klines[-1]
            hist_vols = klines[-(lookback + 1):-1, 5]
            
            avg_vol = np.mean(hist_vols)
            if avg_vol <= 0:
                return self._neutral("Average volume is zero")

            current_vol = last_candle[5]
            vol_ratio = current_vol / avg_vol

            # 1. Volume Spike Check
            if vol_ratio < vol_mult:
                return self._neutral(f"Volume ratio ({vol_ratio:.2f}x) below threshold ({vol_mult}x)")

            # 2. Narrow Range Check
            c_high, c_low, c_open, c_close = last_candle[2], last_candle[3], last_candle[1], last_candle[4]
            candle_range = c_high - c_low
            range_pct = candle_range / c_open if c_open > 0 else 0

            if range_pct > max_range_pct:
                return self._neutral(f"Price range ({range_pct*100:.3f}%) exceeds max ({max_range_pct*100:.3f}%)")

            # 3. Directional Bias
            # Determine where price closed relative to its high/low range
            # Position = (Close - Low) / (High - Low) -> 0.0 to 1.0
            if candle_range <= 0:
                return self._neutral("Zero candle range")
                
            close_pos = (c_close - c_low) / candle_range

            # Bullish Absorption: High vol at lows with narrow range, close near low (buyers defending)
            if close_pos <= 0.3:
                side = "LONG"
                rationale = (
                    f"Bullish Absorption: {vol_ratio:.1f}x Volume spike on narrow range ({range_pct*100:.3f}%). "
                    f"Aggressive sellers absorbed by buyers near candle lows ({close_pos:.2f})."
                )
            # Bearish Absorption: High vol at highs with narrow range, close near high (sellers defending)
            elif close_pos >= 0.7:
                side = "SHORT"
                rationale = (
                    f"Bearish Absorption: {vol_ratio:.1f}x Volume spike on narrow range ({range_pct*100:.3f}%). "
                    f"Aggressive buyers absorbed by sellers near candle highs ({close_pos:.2f})."
                )
            else:
                return self._neutral(f"Indeterminate absorption direction (Close Pos: {close_pos:.2f})")

            # Confidence scales with volume ratio
            # 3x -> 65% Conf, 6x -> 85% Conf
            conf = 65.0 + (vol_ratio - vol_mult) * 6.0
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
                    "range_pct": round(range_pct, 5),
                    "close_pos": round(close_pos, 2)
                }
            )

        except Exception as e:
            logger.error("AbsorptionDetection error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")