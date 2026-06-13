"""
KAVACH-07 Strategy: Regime Filter
Classifies market as Trending / Ranging / Volatile using ADX + ATR.
Acts as a meta-filter for MetaStrategy — does NOT contribute a directional signal.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import Signal, StrategyBase
from ..core.indicators import adx, atr, klines_to_ohlcv, sma_last, std_last

logger = logging.getLogger(__name__)

REGIME_TRENDING = "TRENDING"
REGIME_RANGING  = "RANGING"
REGIME_VOLATILE = "VOLATILE"
REGIME_UNDEFINED = "UNDEFINED"


class RegimeFilter(StrategyBase):
    """Determine market regime and embed it in signal metadata.

    Output ``side`` is always NEUTRAL. The real output is in
    ``signal.metadata["regime"]`` which MetaStrategy reads to
    adjust strategy weights.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None:
            return self._neutral("No market data")

        klines = md.kline_data  # deque of (o,h,l,c,v,...) tuples (5m bars)
        if len(klines) < 30:
            return self._neutral("Insufficient kline history for regime")

        try:
            o, h, l, c, v = klines_to_ohlcv(klines)

            adx_val, plus_di, minus_di = adx(h, l, c, period=14)
            atr_val = atr(h, l, c, period=14)
            avg_atr = sma_last(
                atr(h[:i], l[:i], c[:i], period=14)
                if len(c) > 14
                else atr_val
                for i in range(len(c) - self._cfg.get("atr_lookback_periods", 20), len(c))
            ) if False else _avg_atr_over_n(h, l, c, period=14, n=self._cfg.get("atr_lookback_periods", 20))

            adx_thresh_trending = float(self._cfg.get("adx_threshold_trending", 25))
            adx_thresh_ranging  = float(self._cfg.get("adx_threshold_ranging", 20))
            atr_mult_volatile   = float(self._cfg.get("atr_multiplier_volatile", 1.5))

            # Determine regime
            if atr_val > atr_mult_volatile * avg_atr and avg_atr > 0:
                regime = REGIME_VOLATILE
                confidence = min(100.0, (atr_val / (avg_atr + 1e-10) - 1.0) * 50.0)
            elif adx_val >= adx_thresh_trending:
                regime = REGIME_TRENDING
                confidence = min(100.0, (adx_val - adx_thresh_trending) * 2.0)
            elif adx_val <= adx_thresh_ranging:
                regime = REGIME_RANGING
                confidence = min(100.0, (adx_thresh_ranging - adx_val) * 3.0)
            else:
                regime = REGIME_UNDEFINED
                confidence = 0.0

            direction = ""
            if regime == REGIME_TRENDING:
                direction = " (BULLISH)" if plus_di > minus_di else " (BEARISH)"

            rationale = (
                f"Regime={regime}{direction} | ADX={adx_val:.1f} "
                f"+DI={plus_di:.1f} -DI={minus_di:.1f} | ATR={atr_val:.4f}"
            )
            sig = self._create_signal(
                self.symbol, "NEUTRAL", confidence, 0.0, 0.0, 0.0, rationale
            )
            sig.metadata["regime"] = regime
            sig.metadata["adx"] = adx_val
            sig.metadata["plus_di"] = plus_di
            sig.metadata["minus_di"] = minus_di
            sig.metadata["atr_ratio"] = atr_val / (avg_atr + 1e-10)
            return sig

        except Exception as exc:
            logger.error("RegimeFilter[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Regime error: {exc}")


def _avg_atr_over_n(h, l, c, period: int, n: int) -> float:
    """Compute average ATR over the last n closing bars."""
    import numpy as np
    from ..core.indicators import _true_range, _wilder_smooth
    if len(c) < period + 1:
        return 0.0
    tr = _true_range(h, l, c)
    smoothed = _wilder_smooth(tr, period)
    valid = smoothed[smoothed > 0]
    if len(valid) == 0:
        return 0.0
    window = valid[-n:] if len(valid) >= n else valid
    return float(np.mean(window))
