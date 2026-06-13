"""
KAVACH-07 Strategy: OI Breakout
Signal when OI spikes (N std-devs above MA) AND price breaks recent high/low.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np

from .base import Signal, StrategyBase
from ..core.indicators import (
    klines_to_ohlcv, highest_high, lowest_low, sma_last, std_last,
    sl_long, sl_short, tp_long, tp_short,
)

logger = logging.getLogger(__name__)


class OiBreakout(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        oi_history: list = data_context.get(f"{self.symbol}_oi_history", [])

        lookback  = int(self._cfg.get("oi_lookback_period", 20))
        oi_mult   = float(self._cfg.get("oi_std_dev_multiplier", 2.0))
        pb_period = int(self._cfg.get("price_break_period", 20))
        sl_pct    = float(self._cfg.get("sl_percent", 1.0))
        tp_pct    = float(self._cfg.get("tp_percent", 2.5))

        try:
            klines = md.kline_data
            if len(klines) < pb_period + 5:
                return self._neutral("Insufficient kline history")

            _, h, l, c, _ = klines_to_ohlcv(klines)
            current_price = md.price
            current_oi    = md.open_interest

            if current_oi <= 0:
                return self._neutral("OI data unavailable")

            # ── OI spike detection ──────────────────────────────────────────
            if len(oi_history) < lookback:
                oi_arr = np.array([current_oi] * lookback, dtype=float)
            else:
                oi_arr = np.array(oi_history[-lookback:], dtype=float)

            oi_mean = float(np.mean(oi_arr))
            oi_std  = float(np.std(oi_arr)) + 1e-10
            oi_z_score = (current_oi - oi_mean) / oi_std

            if oi_z_score < oi_mult:
                return self._neutral(
                    f"OI z-score {oi_z_score:.2f} < threshold {oi_mult}"
                )

            # ── Price breakout detection ────────────────────────────────────
            hh = highest_high(h[:-1], pb_period)   # exclude current bar
            ll = lowest_low(l[:-1], pb_period)

            sl_dist = (current_price - ll) / current_price * 100.0  # % distance to swing low
            if current_price > hh and hh > 0:
                # LONG breakout
                breakout_pct = (current_price - hh) / hh * 100.0
                confidence = min(95.0, 55.0 + oi_z_score * 8.0 + breakout_pct * 3.0)
                rationale = (
                    f"OI spike z={oi_z_score:.2f} + Price ABOVE {pb_period}-bar high "
                    f"({hh:.4f}) | OI={current_oi/1e6:.1f}M | Breakout={breakout_pct:.2f}%"
                )
                return self._create_signal(
                    self.symbol, "LONG", confidence, current_price,
                    sl_long(current_price, sl_pct),
                    tp_long(current_price, tp_pct),
                    rationale,
                )

            elif current_price < ll and ll > 0:
                # SHORT breakout
                breakout_pct = (ll - current_price) / ll * 100.0
                confidence = min(95.0, 55.0 + oi_z_score * 8.0 + breakout_pct * 3.0)
                rationale = (
                    f"OI spike z={oi_z_score:.2f} + Price BELOW {pb_period}-bar low "
                    f"({ll:.4f}) | OI={current_oi/1e6:.1f}M | Breakout={breakout_pct:.2f}%"
                )
                return self._create_signal(
                    self.symbol, "SHORT", confidence, current_price,
                    sl_short(current_price, sl_pct),
                    tp_short(current_price, tp_pct),
                    rationale,
                )

            return self._neutral(
                f"OI spike z={oi_z_score:.2f} confirmed but no price breakout yet "
                f"(HH={hh:.4f}, LL={ll:.4f}, price={current_price:.4f})"
            )

        except Exception as exc:
            logger.error("OiBreakout[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
