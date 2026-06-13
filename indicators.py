"""
KAVACH-07 — Technical Indicators (Pure NumPy)
No C-extension dependencies — runs on Oracle Cloud ARM64 out of the box.
All functions accept numpy arrays and return floats or arrays.
"""

from __future__ import annotations

from collections import deque
from typing import Tuple

import numpy as np


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _wilder_smooth(arr: np.ndarray, period: int) -> np.ndarray:
    """Wilder's Smoothing (EMA with alpha = 1/period)."""
    result = np.zeros(len(arr))
    if len(arr) < period:
        return result
    result[period - 1] = np.sum(arr[:period])
    for i in range(period, len(arr)):
        result[i] = result[i - 1] - result[i - 1] / period + arr[i]
    return result


def _ema(arr: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average."""
    k = 2.0 / (period + 1)
    result = np.zeros(len(arr))
    if len(arr) == 0:
        return result
    result[0] = arr[0]
    for i in range(1, len(arr)):
        result[i] = arr[i] * k + result[i - 1] * (1 - k)
    return result


def _sma(arr: np.ndarray, period: int) -> np.ndarray:
    """Simple Moving Average via convolution."""
    if len(arr) < period:
        return np.full(len(arr), np.nan)
    kernel = np.ones(period) / period
    sma = np.convolve(arr, kernel, mode="valid")
    return np.concatenate([np.full(period - 1, np.nan), sma])


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """True Range array. Requires len >= 2."""
    prev_close = close[:-1]
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)),
    )
    return tr


# ──────────────────────────────────────────────────────────────────────────────
# Public Indicators
# ──────────────────────────────────────────────────────────────────────────────

def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    """Average True Range — returns latest scalar value."""
    if len(close) < period + 1:
        return 0.0
    tr = _true_range(high, low, close)
    smoothed = _wilder_smooth(tr, period)
    val = smoothed[-1]
    return float(val) if np.isfinite(val) else 0.0


def adx(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> Tuple[float, float, float]:
    """ADX, +DI, -DI — returns (adx_val, plus_di, minus_di) scalars."""
    if len(close) < 2 * period + 1:
        return 0.0, 0.0, 0.0

    up_move = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = _true_range(high, low, close)

    atr_s = _wilder_smooth(tr, period)
    pdm_s = _wilder_smooth(plus_dm, period)
    mdm_s = _wilder_smooth(minus_dm, period)

    eps = 1e-10
    plus_di_arr = 100.0 * pdm_s / (atr_s + eps)
    minus_di_arr = 100.0 * mdm_s / (atr_s + eps)
    dx = 100.0 * np.abs(plus_di_arr - minus_di_arr) / (plus_di_arr + minus_di_arr + eps)
    adx_arr = _wilder_smooth(dx[period - 1 :], period)

    if len(adx_arr) == 0:
        return 0.0, 0.0, 0.0

    adx_val = float(adx_arr[-1]) if np.isfinite(adx_arr[-1]) else 0.0
    pdi_val = float(plus_di_arr[-1]) if np.isfinite(plus_di_arr[-1]) else 0.0
    mdi_val = float(minus_di_arr[-1]) if np.isfinite(minus_di_arr[-1]) else 0.0
    return adx_val, pdi_val, mdi_val


def vwap_from_klines(klines: deque) -> float:
    """VWAP from kline deque. Each kline: (open, high, low, close, volume, timestamp).
    Resets at the start of each UTC day by design of the deque window.
    """
    if not klines or len(klines) < 1:
        return 0.0
    typical_price_vol = 0.0
    total_volume = 0.0
    for k in klines:
        # k = (open, high, low, close, volume, ...)
        if len(k) < 5:
            continue
        o, h, l, c, v = float(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])
        tp = (h + l + c) / 3.0
        typical_price_vol += tp * v
        total_volume += v
    if total_volume < 1e-10:
        return 0.0
    return typical_price_vol / total_volume


def cvd_from_klines(klines: deque) -> float:
    """Cumulative Volume Delta from kline deque.
    Delta per bar = +volume if close > open, -volume if close < open.
    """
    if not klines:
        return 0.0
    cumulative = 0.0
    for k in klines:
        if len(k) < 5:
            continue
        o, c, v = float(k[0]), float(k[3]), float(k[4])
        if c > o:
            cumulative += v
        elif c < o:
            cumulative -= v
    return cumulative


def rsi(close: np.ndarray, period: int = 14) -> float:
    """RSI — returns latest scalar value."""
    if len(close) < period + 1:
        return 50.0
    delta = np.diff(close)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss < 1e-10:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1 + rs))


def sma_last(arr: np.ndarray, period: int) -> float:
    """Return the last SMA value."""
    import numpy as np
    if len(arr) < period:
        return float(np.mean(arr)) if len(arr) > 0 else 0.0
    return float(np.mean(arr[-period:]))


def std_last(arr: np.ndarray, period: int) -> float:
    """Return std-dev over the last `period` values."""
    if len(arr) < 2:
        return 0.0
    window = arr[-period:] if len(arr) >= period else arr
    return float(np.std(window))


def highest_high(high: np.ndarray, period: int) -> float:
    """Highest high over last `period` bars."""
    if len(high) == 0:
        return 0.0
    return float(np.max(high[-period:]))


def lowest_low(low: np.ndarray, period: int) -> float:
    """Lowest low over last `period` bars."""
    if len(low) == 0:
        return 0.0
    return float(np.min(low[-period:]))


def klines_to_ohlcv(
    klines: deque,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Convert kline deque to (open, high, low, close, volume) arrays."""
    if not klines:
        empty = np.array([], dtype=float)
        return empty, empty, empty, empty, empty
    data = list(klines)
    opens  = np.array([float(k[0]) for k in data], dtype=float)
    highs  = np.array([float(k[1]) for k in data], dtype=float)
    lows   = np.array([float(k[2]) for k in data], dtype=float)
    closes = np.array([float(k[3]) for k in data], dtype=float)
    vols   = np.array([float(k[4]) for k in data], dtype=float)
    return opens, highs, lows, closes, vols


def sl_long(entry: float, sl_pct: float) -> float:
    """Stop-loss price below entry."""
    return round(entry * (1.0 - sl_pct / 100.0), 8)


def sl_short(entry: float, sl_pct: float) -> float:
    """Stop-loss price above entry."""
    return round(entry * (1.0 + sl_pct / 100.0), 8)


def tp_long(entry: float, tp_pct: float) -> float:
    """Take-profit price above entry."""
    return round(entry * (1.0 + tp_pct / 100.0), 8)


def tp_short(entry: float, tp_pct: float) -> float:
    """Take-profit price below entry."""
    return round(entry * (1.0 - tp_pct / 100.0), 8)
