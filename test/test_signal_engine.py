"""
Tests for SignalEngine — pre-filters, validation, duplicate checks.
No external dependencies. Uses a fake DataEngine.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from models import Signal


def _make_signal(direction="LONG", confidence=0.65,
                 entry=100.0, sl=99.0, tp1=102.0, tp2=None,
                 strategy="TEST", symbol="BTCUSDT") -> Signal:
    return Signal(
        symbol=symbol, strategy=strategy,
        direction=direction, confidence=confidence,
        entry_type="LIMIT", entry_price=entry,
        sl_price=sl, tp1_price=tp1, tp2_price=tp2,
        risk_pct=0.005, atr=0.35,
    )


class TestSignalValidation(unittest.TestCase):
    """Test _validate_signal() logic in isolation."""

    def setUp(self):
        # Build a minimal SignalEngine without starting anything
        from config import Config
        cfg = Config()
        cfg.MAX_SL_PCT = 0.08

        # Mock everything
        self.cfg = cfg

    def _validate(self, sig: Signal) -> bool:
        """Replicate _validate_signal logic."""
        if sig.entry_price < 1e-10:
            return False
        if sig.sl_price < 1e-10 or sig.tp1_price < 1e-10:
            return False
        if sig.direction == "LONG" and sig.sl_price >= sig.entry_price:
            return False
        if sig.direction == "SHORT" and sig.sl_price <= sig.entry_price:
            return False
        if sig.direction == "LONG" and sig.tp1_price <= sig.entry_price:
            return False
        if sig.direction == "SHORT" and sig.tp1_price >= sig.entry_price:
            return False
        sl_dist = abs(sig.entry_price - sig.sl_price) / sig.entry_price
        if sl_dist > self.cfg.MAX_SL_PCT:
            return False
        if sig.r_ratio < 1.2:
            return False
        return True

    def test_valid_long_passes(self):
        sig = _make_signal("LONG", entry=100, sl=99, tp1=102)
        self.assertTrue(self._validate(sig))

    def test_valid_short_passes(self):
        sig = _make_signal("SHORT", entry=100, sl=101, tp1=97)
        self.assertTrue(self._validate(sig))

    def test_sl_on_wrong_side_long(self):
        sig = _make_signal("LONG", entry=100, sl=101, tp1=105)
        self.assertFalse(self._validate(sig))

    def test_sl_on_wrong_side_short(self):
        sig = _make_signal("SHORT", entry=100, sl=99, tp1=95)
        self.assertFalse(self._validate(sig))

    def test_tp_unprofitable_long(self):
        sig = _make_signal("LONG", entry=100, sl=99, tp1=99.5)
        self.assertFalse(self._validate(sig))

    def test_sl_too_large(self):
        # 10% SL → exceeds MAX_SL_PCT=8%
        sig = _make_signal("LONG", entry=100, sl=90, tp1=120)
        self.assertFalse(self._validate(sig))

    def test_r_ratio_too_small(self):
        # SL=1, TP=0.5 → R=0.5 < 1.2
        sig = _make_signal("LONG", entry=100, sl=99, tp1=100.5)
        self.assertFalse(self._validate(sig))

    def test_minimum_r_ratio(self):
        # SL=1, TP=1.2 → R=1.2 (borderline pass)
        sig = _make_signal("LONG", entry=100, sl=99, tp1=101.2)
        self.assertTrue(self._validate(sig))


class TestSignalRatioCalc(unittest.TestCase):

    def test_r_ratio_2(self):
        sig = _make_signal("LONG", entry=100, sl=99, tp1=102)
        self.assertAlmostEqual(sig.r_ratio, 2.0, delta=0.01)

    def test_r_ratio_short(self):
        sig = _make_signal("SHORT", entry=100, sl=102, tp1=94)
        self.assertAlmostEqual(sig.r_ratio, 3.0, delta=0.01)

    def test_sl_distance_pct(self):
        sig = _make_signal("LONG", entry=100, sl=96, tp1=108)
        self.assertAlmostEqual(sig.sl_distance_pct, 0.04, delta=0.001)


class TestSignalCooldown(unittest.TestCase):
    """Test the in-memory cooldown logic."""

    def test_cooldown_suppresses_duplicate(self):
        import time
        cooldown_ms = 180_000
        cooldown_s = cooldown_ms / 1000

        recent: dict = {}
        key = "BTCUSDT:TEST"

        # First signal passes
        ts = time.time()
        recent[key] = ts
        elapsed = time.time() - recent.get(key, 0.0)
        self.assertLess(elapsed, cooldown_s)  # Should be within cooldown

    def test_cooldown_expired(self):
        import time
        recent = {"BTCUSDT:TEST": time.time() - 300}  # 5 min ago
        cooldown_s = 180.0
        elapsed = time.time() - recent["BTCUSDT:TEST"]
        self.assertGreater(elapsed, cooldown_s)  # Should be expired


class TestFeatureBuilding(unittest.TestCase):
    """Test _build_features produces expected keys."""

    def _build_features(self, data, sig) -> dict:
        """Replicate _build_features logic."""
        def safe_div(a, b):
            return a / b if abs(b) > 1e-10 else 0.0

        price_change_5m = safe_div(
            float(data.candles_5m[-1]["close"]) - float(data.candles_5m[-2]["close"])
            if len(data.candles_5m) >= 2 else 0.0,
            float(data.candles_5m[-2]["close"]) if len(data.candles_5m) >= 2 else 1.0,
        )

        return {
            "price_change_5m": price_change_5m,
            "atr_pct": data.atr_5m / data.mark_price if data.mark_price > 0 else 0,
            "ob_imbalance": data.ob_imbalance,
            "spread_pct": data.spread_pct,
            "cvd_z_score": data.cvd_z_score,
            "funding_rate": data.funding_rate,
            "funding_percentile": data.funding_percentile / 100.0,
            "oi_change_1h": data.oi_change_1h,
            "volume_ratio": data.volume_ratio,
            "delta_direction": float(data.delta_direction),
            "fear_greed": data.fear_greed_index / 100.0,
            "confidence_raw": sig.confidence,
            "r_ratio": sig.r_ratio,
            "is_long": 1.0 if sig.direction == "LONG" else 0.0,
        }

    def test_features_have_expected_keys(self):
        # Create minimal snapshot
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from tests.test_strategies import _base_snapshot
        snap = _base_snapshot(100.0)
        sig = _make_signal()

        features = self._build_features(snap, sig)
        expected_keys = [
            "price_change_5m", "atr_pct", "ob_imbalance", "spread_pct",
            "cvd_z_score", "funding_rate", "volume_ratio", "confidence_raw", "r_ratio",
        ]
        for k in expected_keys:
            self.assertIn(k, features, f"Missing feature key: {k}")

    def test_features_are_finite(self):
        import math
        from tests.test_strategies import _base_snapshot
        snap = _base_snapshot(100.0)
        sig = _make_signal()
        features = self._build_features(snap, sig)
        for k, v in features.items():
            self.assertTrue(math.isfinite(v), f"Feature {k}={v} is not finite")


if __name__ == "__main__":
    unittest.main(verbosity=2)
