"""
KAVACH-07 — Regime Filter Strategy
Classifies market conditions into TRENDING, RANGING, or VOLATILE.
Weight is 0.0 as it acts as a meta-filter, not a directional signal generator.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np
from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.regime_filter")

class RegimeFilter(StrategyBase):
    """
    Analyzes ADX and ATR to determine the current market regime.
    Used by MetaStrategy to adjust weights of other strategies.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        """
        Determines the regime based on pre-calculated indicators in MarketData.
        Classification Logic:
        1. VOLATILE: Current ATR > ATR_Multiplier * SMA(ATR, N)
        2. TRENDING: ADX > Trending_Threshold (default 25)
        3. RANGING: ADX < Ranging_Threshold (default 20)
        """
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine not warm")

        # Thresholds from config
        adx_trending = float(self._cfg.get("adx_threshold_trending", 25.0))
        adx_ranging = float(self._cfg.get("adx_threshold_ranging", 20.0))
        atr_mult = float(self._cfg.get("atr_multiplier_volatile", 1.5))
        atr_lookback = int(self._cfg.get("atr_lookback_periods", 20))

        try:
            # 1. Volatility Check
            # We need the average ATR over the lookback period
            if len(md.klines_5m) < atr_lookback + 14:
                return self._neutral("Insufficient history for ATR SMA")

            # Extract ATR values from 5m history
            # Note: MarketData stores the latest ADX/ATR, but for the Volatile check
            # we need to ensure we compare current ATR against its recent average.
            # In a production environment, DataEngine would provide this series.
            # Here we derive it from the kline history to ensure accuracy.
            
            klines = np.array(list(md.klines_5m))
            highs = klines[:, 2]
            lows = klines[:, 3]
            closes = klines[:, 4]

            # Calculate ATR series for the lookback window
            def get_atr_series(h, l, c, period=14):
                tr = np.maximum(h[1:] - l[1:], 
                                np.maximum(abs(h[1:] - c[:-1]), 
                                           abs(l[1:] - c[:-1])))
                # Wilder's Smoothing for ATR
                atr_series = np.zeros(len(tr))
                if len(tr) < period: return atr_series
                atr_series[period-1] = np.mean(tr[:period])
                for i in range(period, len(tr)):
                    atr_series[i] = (atr_series[i-1] * (period - 1) + tr[i]) / period
                return atr_series

            atr_series = get_atr_series(highs, lows, closes)
            current_atr = md.atr
            avg_atr = np.mean(atr_series[-atr_lookback:]) if len(atr_series) >= atr_lookback else current_atr

            # Classification
            regime = "UNDEFINED"
            
            if current_atr > (avg_atr * atr_mult) and avg_atr > 0:
                regime = "VOLATILE"
            elif md.adx >= adx_trending:
                regime = "TRENDING"
            elif md.adx <= adx_ranging:
                regime = "RANGING"
            else:
                regime = "NORMAL"

            # Create Neutral signal with regime metadata
            sig = self._neutral(f"Market Regime: {regime}")
            sig.metadata["regime"] = regime
            sig.metadata["adx"] = round(md.adx, 2)
            sig.metadata["atr"] = round(md.atr, 6)
            
            return sig

        except Exception as e:
            logger.error("RegimeFilter error for %s: %s", self.symbol, e)
            return self._neutral(f"Error calculating regime: {str(e)}")