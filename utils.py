"""
KAVACH-07 — Utilities
Logging, ATR, volume profile, statistical helpers, decorators.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import sys
import time
import traceback
from collections import deque
from typing import List, Optional, Callable, Any

import numpy as np


# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-24s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_level: str = "INFO", log_file: Optional[str] = None) -> None:
    """Configure root logger. Call once at startup."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_file:
        os.makedirs(os.path.dirname(log_file) if os.path.dirname(log_file) else ".", exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
        handlers.append(fh)

    logging.basicConfig(level=level, format=_LOG_FORMAT, datefmt=_DATE_FORMAT, handlers=handlers)

    # Silence noisy third-party loggers
    for lib in ("websockets", "aiohttp", "telegram", "httpx", "httpcore"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# ─────────────────────────────────────────────────────────────
# Technical Analysis Helpers
# ─────────────────────────────────────────────────────────────

def calc_atr(candles: List[dict], period: int = 14) -> float:
    """
    Average True Range from candle dicts with keys: high, low, close.
    Returns 0.0 if insufficient data.
    """
    if len(candles) < 2:
        return 0.0

    trs: list[float] = []
    for i in range(1, len(candles)):
        h = float(candles[i]["high"])
        l = float(candles[i]["low"])
        pc = float(candles[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    if not trs:
        return 0.0

    if len(trs) < period:
        return float(np.mean(trs))

    # Wilder's EMA
    atr = float(np.mean(trs[:period]))
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def calc_atr_pct(candles: List[dict], period: int = 14) -> float:
    """ATR as percentage of closing price."""
    if not candles:
        return 0.0
    atr = calc_atr(candles, period)
    close = float(candles[-1]["close"])
    if close < 1e-10:
        return 0.0
    return atr / close


def calc_slope(values: List[float]) -> float:
    """
    Normalised linear regression slope of a value series.
    Returns slope in units of std-devs per sample.
    """
    if len(values) < 3:
        return 0.0
    arr = np.asarray(values, dtype=float)
    std = arr.std()
    if std < 1e-10:
        return 0.0
    x = np.arange(len(arr), dtype=float)
    slope = float(np.polyfit(x, arr, 1)[0])
    return slope / std


def calc_z_score(value: float, history: List[float]) -> float:
    """Z-score of value relative to history."""
    if len(history) < 5:
        return 0.0
    arr = np.asarray(history, dtype=float)
    mean = arr.mean()
    std = arr.std()
    if std < 1e-10:
        return 0.0
    return (value - mean) / std


def calc_percentile(value: float, history: List[float]) -> float:
    """Percentile rank of value within history. Returns 0–100."""
    if not history:
        return 50.0
    arr = np.asarray(history, dtype=float)
    return float(np.sum(arr <= value) / len(arr) * 100)


def calc_volume_profile(
    candles: List[dict], price_bins: int = 50
) -> tuple[float, float, float, list, list]:
    """
    Session volume profile from candle data.
    Returns: (poc, vah, val, lvns, hvns)
    """
    if not candles:
        return 0.0, 0.0, 0.0, [], []

    prices = np.array([float(c["close"]) for c in candles])
    volumes = np.array([float(c["volume"]) for c in candles])

    p_min, p_max = prices.min(), prices.max()
    if p_max - p_min < 1e-10:
        return float(prices[-1]), float(prices[-1]), float(prices[-1]), [], []

    bins = np.linspace(p_min, p_max, price_bins + 1)
    vol_by_bin = np.zeros(price_bins, dtype=float)
    bin_centers = (bins[:-1] + bins[1:]) / 2

    for price, vol in zip(prices, volumes):
        idx = min(int((price - p_min) / (p_max - p_min) * price_bins), price_bins - 1)
        vol_by_bin[idx] += vol

    total_vol = vol_by_bin.sum()
    if total_vol < 1e-10:
        return float(prices[-1]), float(prices[-1]), float(prices[-1]), [], []

    # POC
    poc_idx = int(vol_by_bin.argmax())
    poc = float(bin_centers[poc_idx])

    # Value Area (70% of volume)
    sorted_idx = vol_by_bin.argsort()[::-1]
    va_vol, va_indices = 0.0, []
    for idx in sorted_idx:
        va_vol += vol_by_bin[idx]
        va_indices.append(int(idx))
        if va_vol >= 0.70 * total_vol:
            break

    vah = float(bin_centers[max(va_indices)])
    val = float(bin_centers[min(va_indices)])

    # LVNs = below 20th percentile volume (exclude zero bins)
    nonzero = vol_by_bin[vol_by_bin > 0]
    if len(nonzero) == 0:
        return poc, vah, val, [], []

    p20 = float(np.percentile(nonzero, 20))
    p80 = float(np.percentile(nonzero, 80))

    lvns = [float(bin_centers[i]) for i in range(price_bins) if 0 < vol_by_bin[i] <= p20]
    hvns = [float(bin_centers[i]) for i in range(price_bins) if vol_by_bin[i] >= p80]

    return poc, vah, val, lvns, hvns


def find_swing_high(candles: List[dict], lookback: int = 20) -> float:
    """Highest high in lookback candles."""
    if not candles:
        return 0.0
    window = candles[-min(lookback, len(candles)):]
    return max(float(c["high"]) for c in window)


def find_swing_low(candles: List[dict], lookback: int = 20) -> float:
    """Lowest low in lookback candles."""
    if not candles:
        return 0.0
    window = candles[-min(lookback, len(candles)):]
    return min(float(c["low"]) for c in window)


def calc_volume_ratio(candles: List[dict], period: int = 20) -> float:
    """Current volume vs n-period average volume."""
    if len(candles) < 2:
        return 1.0
    vols = [float(c["volume"]) for c in candles]
    current = vols[-1]
    avg = float(np.mean(vols[-period - 1 : -1])) if len(vols) > 1 else current
    if avg < 1e-10:
        return 1.0
    return current / avg


def price_change_pct(candles: List[dict], n_candles: int = 5) -> float:
    """% price change over last n_candles."""
    if len(candles) < n_candles + 1:
        return 0.0
    start = float(candles[-(n_candles + 1)]["close"])
    end = float(candles[-1]["close"])
    if start < 1e-10:
        return 0.0
    return (end - start) / start


# ─────────────────────────────────────────────────────────────
# Decorators
# ─────────────────────────────────────────────────────────────

def retry_async(max_attempts: int = 3, base_delay: float = 1.0, exceptions=(Exception,)):
    """Retry an async function with exponential backoff."""
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            delay = base_delay
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt < max_attempts:
                        await asyncio.sleep(delay)
                        delay *= 2
            raise last_exc  # type: ignore
        return wrapper
    return decorator


def log_exceptions(logger_name: str):
    """Decorator: log any exception from an async function without crashing."""
    logger = get_logger(logger_name)

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"Unhandled exception in {fn.__name__}:\n{traceback.format_exc()}")
                return None
        return wrapper
    return decorator


# ─────────────────────────────────────────────────────────────
# Rate Limiter
# ─────────────────────────────────────────────────────────────

class RateLimiter:
    """Token bucket rate limiter for REST API calls."""

    def __init__(self, rate: int = 1200, per_seconds: float = 60.0):
        self._rate = rate
        self._per = per_seconds
        self._tokens = float(rate)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(
                self._rate,
                self._tokens + elapsed * (self._rate / self._per),
            )
            if self._tokens >= tokens:
                self._tokens -= tokens
                return
            wait = (tokens - self._tokens) / (self._rate / self._per)
            await asyncio.sleep(wait)
            self._tokens = 0


# ─────────────────────────────────────────────────────────────
# Rolling Buffer with stats
# ─────────────────────────────────────────────────────────────

class RollingBuffer:
    """Thread-safe deque with mean/std helpers."""

    def __init__(self, maxlen: int):
        self._buf: deque[float] = deque(maxlen=maxlen)

    def append(self, value: float) -> None:
        self._buf.append(value)

    def __len__(self) -> int:
        return len(self._buf)

    def to_list(self) -> List[float]:
        return list(self._buf)

    @property
    def mean(self) -> float:
        if not self._buf:
            return 0.0
        return float(np.mean(list(self._buf)))

    @property
    def std(self) -> float:
        if len(self._buf) < 2:
            return 0.0
        return float(np.std(list(self._buf)))

    def z_score(self, value: float) -> float:
        std = self.std
        if std < 1e-10:
            return 0.0
        return (value - self.mean) / std

    def percentile(self, value: float) -> float:
        if not self._buf:
            return 50.0
        arr = np.asarray(list(self._buf))
        return float(np.sum(arr <= value) / len(arr) * 100)
