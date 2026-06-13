"""
KAVACH-07 Strategy: Absorption Detection
Detect high volume with minimal price movement (absorption of large orders).
Absorption at support → LONG. Absorption at resistance → SHORT.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np

from .base import Signal, StrategyBase
from ..core.indicators import (
    klines_to_ohlcv, sma_last, sl_long, sl_short, tp_long, tp_short,
)

logger = logging.getLogger(__name__)


class AbsorptionDetection(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        vol_mult     = float(self._cfg.get("volume_spike_multiplier", 3.0))
        lookback     = int(self._cfg.get("lookback_bars", 20))
        price_thresh = float(self._cfg.get("price_range_threshold_percent", 0.1)) / 100.0
        sl_pct       = float(self._cfg.get("sl_percent", 0.8))
        tp_pct       = float(self._cfg.get("tp_percent", 1.6))

        try:
            klines = md.kline_data
            if len(klines) < lookback + 5:
                return self._neutral("Insufficient kline history for absorption")

            o, h, l, c, v = klines_to_ohlcv(klines)

            # Last bar absorption check
            last_open  = o[-1]
            last_high  = h[-1]
            last_low   = l[-1]
            last_close = c[-1]
            last_vol   = v[-1]

            # Average volume over lookback
            avg_vol = sma_last(v[:-1], lookback)
            if avg_vol < 1e-10:
                return self._neutral("Zero average volume")

            # Volume spike check
            vol_ratio = last_vol / avg_vol
            if vol_ratio < vol_mult:
                return self._neutral(
                    f"Volume ratio {vol_ratio:.2f}x < {vol_mult}x threshold"
                )

            # Price range relative to typical range (absorption = high vol, small range)
            bar_range_pct = (last_high - last_low) / (last_open + 1e-10)
            avg_range_pct = float(np.mean(
                (h[i] - l[i]) / (o[i] + 1e-10)
                for i in range(-lookback - 1, -1)
                if o[i] > 0
            )) if lookback > 0 else bar_range_pct

            is_narrow_bar = bar_range_pct < price_thresh or bar_range_pct < avg_range_pct * 0.5

            if not is_narrow_bar:
                return self._neutral(
                    f"High volume ({vol_ratio:.1f}x) but wide bar range "
                    f"({bar_range_pct*100:.2f}%) — not absorption"
                )

            # Determine if at support (close near low = buying absorption)
            # or resistance (close near high = selling absorption)
            bar_size = last_high - last_low
            if bar_size < 1e-10:
                return self._neutral("Zero bar size")

            close_position = (last_close - last_low) / bar_size  # 0=at low, 1=at high

            price = md.price
            confidence_base = min(90.0, 50.0 + (vol_ratio - vol_mult) * 8.0)

            if close_position < 0.35:
                # Closed near low with massive volume → BUYING absorption → LONG
                confidence = confidence_base + (0.35 - close_position) * 40.0
                rationale = (
                    f"BUY ABSORPTION: Vol {vol_ratio:.1f}x avg, narrow bar "
                    f"({bar_range_pct*100:.2f}%), closed near LOW ({close_position:.2f}) "
                    f"→ Support holding → LONG"
                )
                return self._create_signal(
                    self.symbol, "LONG", min(93.0, confidence), price,
                    sl_long(price, sl_pct), tp_long(price, tp_pct), rationale
                )

            elif close_position > 0.65:
                # Closed near high with massive volume → SELLING absorption → SHORT
                confidence = confidence_base + (close_position - 0.65) * 40.0
                rationale = (
                    f"SELL ABSORPTION: Vol {vol_ratio:.1f}x avg, narrow bar "
                    f"({bar_range_pct*100:.2f}%), closed near HIGH ({close_position:.2f}) "
                    f"→ Resistance holding → SHORT"
                )
                return self._create_signal(
                    self.symbol, "SHORT", min(93.0, confidence), price,
                    sl_short(price, sl_pct), tp_short(price, tp_pct), rationale
                )

            return self._neutral(
                f"High volume ({vol_ratio:.1f}x) narrow bar but indeterminate "
                f"close position {close_position:.2f}"
            )

        except Exception as exc:
            logger.error("AbsorptionDetection[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
