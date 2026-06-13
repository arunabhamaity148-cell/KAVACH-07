"""
KAVACH-07 — CVD Divergence Strategy
Detects divergences between price action and Cumulative Volume Delta (CVD).
Bullish: Price Lower Low + CVD Higher Low.
Bearish: Price Higher High + CVD Lower High.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

import numpy as np
from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.cvd_divergence")

class CvdDivergence(StrategyBase):
    """
    Logic:
    1. Extract price and cumulative CVD series from 5m kline history.
    2. Identify local swing highs and lows within the lookback window.
    3. Check for standard divergences between price swings and CVD swings.
    4. Minimum divergence length: 5 bars. Lookback: 30 bars.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        lookback = int(self._cfg.get("lookback_bars", 30))
        min_bars = int(self._cfg.get("min_divergence_bars", 5))
        sl_pct = float(self._cfg.get("sl_percent", 1.0)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 2.0)) / 100.0

        try:
            if len(md.klines_5m) < lookback + 5:
                return self._neutral("Insufficient history for divergence analysis")

            # Extract window
            klines = np.array(list(md.klines_5m))[-lookback:]
            prices = klines[:, 4]  # Closes
            # We need the CVD series. Since DataEngine updates md.cvd cumulatively,
            # we need to reconstruct the series for the window.
            # Delta = vol if close > open else -vol
            deltas = np.where(klines[:, 4] >= klines[:, 1], klines[:, 5], -klines[:, 5])
            cvd_series = np.cumsum(deltas)

            # Find local extrema
            # A swing low is a point lower than 2 points on either side
            def find_extrema(series: np.ndarray, is_max: bool) -> List[int]:
                indices = []
                for i in range(2, len(series) - 2):
                    if is_max:
                        if series[i] == np.max(series[i-2:i+3]):
                            indices.append(i)
                    else:
                        if series[i] == np.min(series[i-2:i+3]):
                            indices.append(i)
                return indices

            lows = find_extrema(prices, is_max=False)
            highs = find_extrema(prices, is_max=True)

            # Check Bullish Divergence (Price LL, CVD HL)
            if len(lows) >= 2:
                p1, p2 = lows[-2], lows[-1]
                if (p2 - p1) >= min_bars:
                    price_ll = prices[p2] < prices[p1]
                    cvd_hl = cvd_series[p2] > cvd_series[p1]
                    
                    if price_ll and cvd_hl:
                        return self._create_div_signal("LONG", prices[p2], cvd_series[p2], md.price, sl_pct, tp_pct)

            # Check Bearish Divergence (Price HH, CVD LH)
            if len(highs) >= 2:
                p1, p2 = highs[-2], highs[-1]
                if (p2 - p1) >= min_bars:
                    price_hh = prices[p2] > prices[p1]
                    cvd_lh = cvd_series[p2] < cvd_series[p1]
                    
                    if price_hh and cvd_lh:
                        return self._create_div_signal("SHORT", prices[p2], cvd_series[p2], md.price, sl_pct, tp_pct)

            return self._neutral("No valid divergence found")

        except Exception as e:
            logger.error("CvdDivergence error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")

    def _create_div_signal(self, side: str, p_ext: float, c_ext: float, entry: float, sl_pct: float, tp_pct: float) -> Signal:
        # Confidence calculation
        conf = 70.0 # Base
        
        if side == "LONG":
            sl = entry * (1.0 - sl_pct)
            tp = entry * (1.0 + tp_pct)
            rationale = f"Bullish CVD Divergence: Price Lower Low vs CVD Higher Low at {p_ext:.6g}."
        else:
            sl = entry * (1.0 + sl_pct)
            tp = entry * (1.0 - tp_pct)
            rationale = f"Bearish CVD Divergence: Price Higher High vs CVD Lower High at {p_ext:.6g}."

        return self._create_signal(
            side=side,
            confidence=conf,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            rationale=rationale
        )