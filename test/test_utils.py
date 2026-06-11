"""Tests for utils.py — ATR, volume profile, statistical helpers."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from utils import (
    calc_atr, calc_atr_pct, calc_slope, calc_z_score,
    calc_percentile, calc_volume_profile, find_swing_high,
    find_swing_low, calc_volume_ratio, RollingBuffer,
)


def _make_candles(prices: list, volume: float = 100.0) -> list:
    """Build fake candle dicts from a list of close prices."""
    candles = []
    for i, p in enumerate(prices):
        candles.append({
            "open": p * 0.999, "high": p * 1.005,
            "low": p * 0.995,  "close": p,
            "volume": volume, "open_time": i * 60_000,
        })
    return candles


class TestATR(unittest.TestCase):

    def test_atr_returns_zero_for_insufficient_data(self):
        self.assertEqual(calc_atr([], 14), 0.0)
        self.assertEqual(calc_atr(_make_candles([100.0]), 14), 0.0)

    def test_atr_flat_market(self):
        """Flat prices → small ATR proportional to wick."""
        c = _make_candles([100.0] * 30)
        atr = calc_atr(c, 14)
        # With 0.5% high/low range, ATR ≈ 0.5
        self.assertAlmostEqual(atr, 1.0, delta=0.5)

    def test_atr_volatile_market(self):
        import random
        random.seed(42)
        prices = [100.0 + random.gauss(0, 3) for _ in range(50)]
        c = _make_candles(prices)
        atr = calc_atr(c, 14)
        self.assertGreater(atr, 0.5)

    def test_atr_pct(self):
        c = _make_candles([100.0] * 20)
        pct = calc_atr_pct(c, 14)
        self.assertGreater(pct, 0.0)
        self.assertLess(pct, 0.05)

    def test_atr_with_fewer_than_period(self):
        """Should use mean of available true ranges."""
        c = _make_candles([100, 101, 99, 102], 14)
        atr = calc_atr(c, 14)
        self.assertGreater(atr, 0.0)


class TestSlope(unittest.TestCase):

    def test_upward_slope_positive(self):
        vals = [float(i) for i in range(20)]
        s = calc_slope(vals)
        self.assertGreater(s, 0.0)

    def test_downward_slope_negative(self):
        vals = [float(20 - i) for i in range(20)]
        s = calc_slope(vals)
        self.assertLess(s, 0.0)

    def test_flat_slope_near_zero(self):
        vals = [100.0] * 20
        s = calc_slope(vals)
        self.assertEqual(s, 0.0)

    def test_insufficient_data(self):
        self.assertEqual(calc_slope([]), 0.0)
        self.assertEqual(calc_slope([1.0, 2.0]), 0.0)


class TestZScore(unittest.TestCase):

    def test_z_score_at_mean(self):
        hist = [float(i) for i in range(100)]
        z = calc_z_score(50.0, hist)
        self.assertAlmostEqual(abs(z), 0.0, delta=0.2)

    def test_z_score_extreme_value(self):
        hist = [float(i) for i in range(100)]
        z = calc_z_score(200.0, hist)
        self.assertGreater(z, 3.0)

    def test_z_score_needs_history(self):
        z = calc_z_score(50.0, [50.0])
        self.assertEqual(z, 0.0)


class TestVolumeProfile(unittest.TestCase):

    def test_returns_poc_in_range(self):
        prices = list(range(100, 200))
        c = _make_candles(prices)
        poc, vah, val, lvns, hvns = calc_volume_profile(c)
        self.assertGreater(poc, 0)
        self.assertGreaterEqual(vah, val)
        self.assertLess(val, 200)
        self.assertGreater(vah, 100)

    def test_empty_candles(self):
        poc, vah, val, lvns, hvns = calc_volume_profile([])
        self.assertEqual(poc, 0.0)

    def test_single_price(self):
        c = _make_candles([100.0] * 10)
        poc, vah, val, lvns, hvns = calc_volume_profile(c)
        self.assertEqual(poc, 100.0)

    def test_vah_above_val(self):
        import random
        random.seed(7)
        prices = [100 + random.gauss(0, 5) for _ in range(100)]
        c = _make_candles(prices)
        _, vah, val, _, _ = calc_volume_profile(c)
        self.assertGreater(vah, val)


class TestSwingLevels(unittest.TestCase):

    def test_swing_high(self):
        c = _make_candles([100, 105, 102, 98, 103], 1.0)
        # Highest high ≈ 105 * 1.005
        sh = find_swing_high(c, 20)
        self.assertGreaterEqual(sh, 105.0)

    def test_swing_low(self):
        c = _make_candles([100, 98, 95, 102, 100], 1.0)
        sl = find_swing_low(c, 20)
        self.assertLessEqual(sl, 95.0)

    def test_lookback_limited(self):
        c = _make_candles(list(range(1, 50)), 1.0)
        sh5 = find_swing_high(c, 5)
        sh20 = find_swing_high(c, 20)
        self.assertLessEqual(sh5, sh20)


class TestVolumeRatio(unittest.TestCase):

    def test_spike(self):
        """Last candle volume is 5x average."""
        c = _make_candles([100.0] * 25, volume=100.0)
        c[-1]["volume"] = 500.0
        ratio = calc_volume_ratio(c, 20)
        self.assertAlmostEqual(ratio, 5.0, delta=0.5)

    def test_flat(self):
        c = _make_candles([100.0] * 25, volume=100.0)
        ratio = calc_volume_ratio(c, 20)
        self.assertAlmostEqual(ratio, 1.0, delta=0.1)


class TestRollingBuffer(unittest.TestCase):

    def test_maxlen_enforced(self):
        buf = RollingBuffer(maxlen=10)
        for i in range(20):
            buf.append(float(i))
        self.assertEqual(len(buf), 10)

    def test_mean(self):
        buf = RollingBuffer(maxlen=100)
        for i in range(1, 11):
            buf.append(float(i))
        self.assertAlmostEqual(buf.mean, 5.5, delta=0.01)

    def test_z_score(self):
        buf = RollingBuffer(maxlen=200)
        for i in range(100):
            buf.append(float(i))
        z = buf.z_score(50.0)
        self.assertAlmostEqual(abs(z), 0.0, delta=0.5)

    def test_percentile(self):
        buf = RollingBuffer(maxlen=1000)
        for i in range(100):
            buf.append(float(i))
        pct = buf.percentile(50.0)
        self.assertAlmostEqual(pct, 50.0, delta=3.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
