"""
KAVACH-07 — Technical Indicators
Pure NumPy implementation of core signals for maximum performance on 1 OCPU.
"""

from __future__ import annotations
from collections import deque
import numpy as np

def tr(high: np.ndarray, low: np.ndarray, close_prev: np.ndarray) -> np.ndarray:
    """True Range calculation."""
    return np.maximum(high - low, np.maximum(abs(high - close_prev), abs(low - close_prev)))

def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    """Average True Range (Wilder's Smoothing)."""
    if len(close) < period + 1: return 0.0
    true_ranges = tr(high[1:], low[1:], close[:-1])
    # Initial SMA
    atr_val = np.mean(true_ranges[:period])
    # Wilder's Smoothing
    for i in range(period, len(true_ranges)):
        atr_val = (atr_val * (period - 1) + true_ranges[i]) / period
    return float(atr_val)

def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    """Average Directional Index."""
    if len(close) < period * 2: return 0.0
    
    up_move = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]
    
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    
    true_ranges = tr(high[1:], low[1:], close[:-1])
    
    # Smoothed components
    def smooth(series):
        res = np.zeros(len(series))
        res[period-1] = np.mean(series[:period])
        for i in range(period, len(series)):
            res[i] = (res[i-1] * (period - 1) + series[i]) / period
        return res

    s_pdm = smooth(plus_dm)
    s_mdm = smooth(minus_dm)
    s_tr = smooth(true_ranges)
    
    # +DI and -DI
    plus_di = 100 * (s_pdm / (s_tr + 1e-9))
    minus_di = 100 * (s_mdm / (s_tr + 1e-9))
    
    # DX and ADX
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di + 1e-9)
    adx_series = smooth(dx[period-1:])
    
    return float(adx_series[-1])

def vwap_from_klines(klines: deque) -> float:
    """Calculates Volume Weighted Average Price from a kline buffer."""
    if not klines: return 0.0
    arr = np.array(list(klines))
    # [time, o, h, l, c, v]
    tp = (arr[:, 2] + arr[:, 3] + arr[:, 4]) / 3.0
    return float(np.sum(tp * arr[:, 5]) / np.sum(arr[:, 5]))

def cvd_from_klines(klines: deque) -> float:
    """Calculates Cumulative Volume Delta from a kline buffer (Tick Rule)."""
    if not klines: return 0.0
    arr = np.array(list(klines))
    # Delta = Volume if close > open, else -Volume
    deltas = np.where(arr[:, 4] > arr[:, 1], arr[:, 5], -arr[:, 5])
    return float(np.sum(deltas))