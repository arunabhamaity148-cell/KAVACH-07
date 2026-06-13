"""
KAVACH-07 Strategy: Spot Accumulation
Large spot volume with rising price → accumulation → LONG.
Large spot volume with falling price → distribution → SHORT.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import Signal, StrategyBase
from ..core.indicators import klines_to_ohlcv, sma_last, sl_long, sl_short, tp_long, tp_short

logger = logging.getLogger(__name__)


class SpotAccumulation(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        vol_mult = float(self._cfg.get("volume_spike_multiplier", 2.5))
        lookback = int(self._cfg.get("lookback_bars", 20))
        sl_pct   = float(self._cfg.get("sl_percent", 1.0))
        tp_pct   = float(self._cfg.get("tp_percent", 2.0))

        spot_volume = md.spot_volume
        if spot_volume <= 0:
            return self._neutral("Spot volume data unavailable")

        try:
            klines = md.kline_data
            if len(klines) < lookback + 5:
                return self._neutral("Insufficient kline history")

            o, h, l, c, v = klines_to_ohlcv(klines)

            # Compare spot volume against futures volume average
            avg_futures_vol = sma_last(v[:-1], lookback)
            if avg_futures_vol < 1e-10:
                return self._neutral("Zero avg futures volume")

            spot_ratio = spot_volume / avg_futures_vol

            if spot_ratio < vol_mult:
                return self._neutral(
                    f"Spot volume ratio {spot_ratio:.2f}x < {vol_mult}x threshold"
                )

            # Price direction over last N bars
            n_bars = min(5, len(c) - 1)
            price_change = (c[-1] - c[-n_bars]) / (c[-n_bars] + 1e-10)
            price = md.price
            confidence_base = min(78.0, 40.0 + (spot_ratio - vol_mult) * 8.0)

            if price_change > 0.002:
                # Rising price + high spot volume → accumulation
                confidence = min(80.0, confidence_base + price_change * 200.0)
                rationale = (
                    f"SPOT ACCUMULATION: Spot vol {spot_ratio:.1f}x avg | "
                    f"Price +{price_change*100:.2f}% over {n_bars} bars → Institutional buying → LONG"
                )
                return self._create_signal(
                    self.symbol, "LONG", confidence, price,
                    sl_long(price, sl_pct), tp_long(price, tp_pct), rationale
                )

            elif price_change < -0.002:
                # Falling price + high spot volume → distribution
                confidence = min(80.0, confidence_base + abs(price_change) * 200.0)
                rationale = (
                    f"SPOT DISTRIBUTION: Spot vol {spot_ratio:.1f}x avg | "
                    f"Price {price_change*100:.2f}% over {n_bars} bars → Institutional selling → SHORT"
                )
                return self._create_signal(
                    self.symbol, "SHORT", confidence, price,
                    sl_short(price, sl_pct), tp_short(price, tp_pct), rationale
                )

            return self._neutral(
                f"High spot volume ({spot_ratio:.1f}x) but neutral price action "
                f"({price_change*100:.2f}%)"
            )

        except Exception as exc:
            logger.error("SpotAccumulation[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
