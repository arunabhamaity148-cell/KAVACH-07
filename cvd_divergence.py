"""
KAVACH-07 Strategy: CVD Divergence
Detect divergence between price direction and Cumulative Volume Delta.
Bullish divergence (price LL, CVD HL) → hidden accumulation → LONG.
Bearish divergence (price HH, CVD LH) → hidden distribution → SHORT.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np

from .base import Signal, StrategyBase
from ..core.indicators import klines_to_ohlcv, rsi, sl_long, sl_short, tp_long, tp_short

logger = logging.getLogger(__name__)


class CvdDivergence(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        lookback    = int(self._cfg.get("lookback_bars", 30))
        min_bars    = int(self._cfg.get("min_divergence_bars", 5))
        sl_pct      = float(self._cfg.get("sl_percent", 1.0))
        tp_pct      = float(self._cfg.get("tp_percent", 2.2))

        try:
            klines = md.kline_data
            if len(klines) < lookback + 5:
                return self._neutral("Insufficient kline history for CVD divergence")

            o, h, l, c, v = klines_to_ohlcv(klines)

            # Build CVD series bar-by-bar
            cvd_series = np.zeros(len(c))
            for i in range(len(c)):
                delta = v[i] if c[i] > o[i] else (-v[i] if c[i] < o[i] else 0.0)
                cvd_series[i] = cvd_series[i - 1] + delta if i > 0 else delta

            # Use last `lookback` bars
            close_window = c[-lookback:]
            cvd_window   = cvd_series[-lookback:]
            low_window   = l[-lookback:]
            high_window  = h[-lookback:]

            # ── Find local swing lows/highs (simple: compare with neighbors) ──
            def find_swing_lows(arr, n_neighbors=3):
                lows_idx = []
                for i in range(n_neighbors, len(arr) - n_neighbors):
                    if arr[i] == min(arr[i - n_neighbors: i + n_neighbors + 1]):
                        lows_idx.append(i)
                return lows_idx

            def find_swing_highs(arr, n_neighbors=3):
                highs_idx = []
                for i in range(n_neighbors, len(arr) - n_neighbors):
                    if arr[i] == max(arr[i - n_neighbors: i + n_neighbors + 1]):
                        highs_idx.append(i)
                return highs_idx

            swing_lows  = find_swing_lows(close_window)
            swing_highs = find_swing_highs(close_window)

            bullish_div = False
            bearish_div = False
            div_strength = 0.0

            # ── Bullish Divergence: price lower low, CVD higher low ──────────
            if len(swing_lows) >= 2:
                prev_low_idx = swing_lows[-2]
                curr_low_idx = swing_lows[-1]
                gap = curr_low_idx - prev_low_idx
                if gap >= min_bars:
                    price_ll = close_window[curr_low_idx] < close_window[prev_low_idx]
                    cvd_hl   = cvd_window[curr_low_idx]   > cvd_window[prev_low_idx]
                    if price_ll and cvd_hl:
                        price_drop  = (close_window[prev_low_idx] - close_window[curr_low_idx]) / (close_window[prev_low_idx] + 1e-10)
                        cvd_rise    = (cvd_window[curr_low_idx] - cvd_window[prev_low_idx]) / (abs(cvd_window[prev_low_idx]) + 1e-10)
                        div_strength = price_drop * 50.0 + abs(cvd_rise) * 50.0
                        bullish_div = True

            # ── Bearish Divergence: price higher high, CVD lower high ────────
            if len(swing_highs) >= 2:
                prev_high_idx = swing_highs[-2]
                curr_high_idx = swing_highs[-1]
                gap = curr_high_idx - prev_high_idx
                if gap >= min_bars:
                    price_hh = close_window[curr_high_idx] > close_window[prev_high_idx]
                    cvd_lh   = cvd_window[curr_high_idx]   < cvd_window[prev_high_idx]
                    if price_hh and cvd_lh:
                        price_rise  = (close_window[curr_high_idx] - close_window[prev_high_idx]) / (close_window[prev_high_idx] + 1e-10)
                        cvd_drop    = (cvd_window[prev_high_idx] - cvd_window[curr_high_idx]) / (abs(cvd_window[prev_high_idx]) + 1e-10)
                        bear_strength = price_rise * 50.0 + abs(cvd_drop) * 50.0
                        if not bullish_div or bear_strength > div_strength:
                            bearish_div  = True
                            bullish_div  = False
                            div_strength = bear_strength

            # ── RSI confirmation ─────────────────────────────────────────────
            rsi_val = rsi(c, period=14)

            price = md.price
            if bullish_div:
                rsi_bonus = 10.0 if rsi_val < 40 else 0.0
                confidence = min(90.0, 50.0 + div_strength * 20.0 + rsi_bonus)
                rationale = (
                    f"BULLISH CVD Divergence: price LL but CVD HL over {gap} bars | "
                    f"div_strength={div_strength:.2f} | RSI={rsi_val:.1f}"
                )
                return self._create_signal(
                    self.symbol, "LONG", confidence, price,
                    sl_long(price, sl_pct), tp_long(price, tp_pct), rationale
                )

            elif bearish_div:
                rsi_bonus = 10.0 if rsi_val > 60 else 0.0
                confidence = min(90.0, 50.0 + div_strength * 20.0 + rsi_bonus)
                rationale = (
                    f"BEARISH CVD Divergence: price HH but CVD LH over {gap} bars | "
                    f"div_strength={div_strength:.2f} | RSI={rsi_val:.1f}"
                )
                return self._create_signal(
                    self.symbol, "SHORT", confidence, price,
                    sl_short(price, sl_pct), tp_short(price, tp_pct), rationale
                )

            return self._neutral(
                f"No CVD divergence detected in last {lookback} bars | CVD={md.cvd:.2f}"
            )

        except Exception as exc:
            logger.error("CvdDivergence[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
