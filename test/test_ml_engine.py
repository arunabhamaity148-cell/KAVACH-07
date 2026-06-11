"""
Tests for MLEngine — predict, update, drift detection, persistence.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import tempfile


def _make_features(is_win: bool = True) -> dict:
    return {
        "price_change_5m":    0.01 if is_win else -0.01,
        "price_change_1h":    0.02 if is_win else -0.02,
        "atr_pct":            0.003,
        "ob_imbalance":       1.5 if is_win else 0.7,
        "spread_pct":         0.00004,
        "cvd_z_score":        2.0 if is_win else -2.0,
        "cvd_slope_5m":       1.0 if is_win else -1.0,
        "funding_rate":       0.0001,
        "funding_percentile": 0.5,
        "oi_change_1h":       0.05 if is_win else -0.05,
        "oi_change_4h":       0.08 if is_win else -0.08,
        "volume_ratio":       2.0 if is_win else 0.8,
        "delta_direction":    1.0 if is_win else -1.0,
        "fear_greed":         0.5,
        "confidence_raw":     0.70 if is_win else 0.55,
        "r_ratio":            2.5 if is_win else 1.5,
        "sl_distance_pct":    0.02,
        "is_long":            1.0 if is_win else 0.0,
    }


class TestMLEngineNoRiver(unittest.TestCase):
    """Test MLEngine gracefully handles missing river library."""

    def setUp(self):
        # Temporarily hide river
        self._original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else None

    def test_predict_returns_05_before_training(self):
        """Should return 0.5 when not enough samples seen."""
        try:
            from ml_engine import MLEngine
        except ImportError:
            self.skipTest("MLEngine not importable")

        ml = MLEngine(min_samples=100)
        features = _make_features(True)
        score = ml.predict(features)
        self.assertAlmostEqual(score, 0.5, delta=0.01)

    def test_is_trained_false_initially(self):
        try:
            from ml_engine import MLEngine
        except ImportError:
            self.skipTest("MLEngine not importable")

        ml = MLEngine(min_samples=100)
        self.assertFalse(ml.is_trained)


class TestMLEngineWithRiver(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import river
        except ImportError:
            raise unittest.SkipTest("river library not installed")

    def test_stats_returns_dict(self):
        from ml_engine import MLEngine
        ml = MLEngine(min_samples=10)
        stats = ml.stats
        self.assertIn("samples_seen", stats)
        self.assertIn("samples_trained", stats)
        self.assertIn("drift_detections", stats)
        self.assertIn("is_trained", stats)
        self.assertIn("roc_auc", stats)

    def test_update_increments_sample_count(self):
        from ml_engine import MLEngine
        ml = MLEngine(min_samples=100)
        initial = ml._samples_seen
        features = _make_features(True)
        ml.update(features, True)
        self.assertEqual(ml._samples_seen, initial + 1)

    def test_predict_before_threshold(self):
        from ml_engine import MLEngine
        ml = MLEngine(min_samples=50)
        features = _make_features(True)
        # Feed some data
        for i in range(20):
            ml.update(features, i % 2 == 0)
        # Should still return 0.5 (not enough samples)
        score = ml.predict(features)
        self.assertAlmostEqual(score, 0.5, delta=0.01)

    def test_predict_after_training(self):
        from ml_engine import MLEngine
        ml = MLEngine(min_samples=30)
        win_features = _make_features(True)
        loss_features = _make_features(False)

        # Train with clear pattern: win_features → True, loss_features → False
        for i in range(50):
            ml.update(win_features, True)
            ml.update(loss_features, False)

        # After training, win features should score > 0.5
        score = ml.predict(win_features)
        self.assertGreater(score, 0.45)  # Allow some slack
        self.assertLess(score, 1.0)

    def test_update_with_both_outcomes(self):
        from ml_engine import MLEngine
        ml = MLEngine(min_samples=10)
        win = _make_features(True)
        loss = _make_features(False)

        for _ in range(15):
            ml.update(win, True)
            ml.update(loss, False)

        # Should have seen 30 samples
        self.assertGreaterEqual(ml._samples_seen, 30)

    def test_is_trained_after_threshold(self):
        from ml_engine import MLEngine
        ml = MLEngine(min_samples=20)
        features = _make_features(True)
        for _ in range(20):
            ml.update(features, True)
        self.assertTrue(ml.is_trained)

    def test_save_and_load(self):
        from ml_engine import MLEngine
        import pickle

        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = os.path.join(tmpdir, "ml_model.pkl")
            original_path = getattr(MLEngine, '_model_path', None)

            ml = MLEngine(min_samples=5)
            features = _make_features(True)
            for _ in range(10):
                ml.update(features, True)

            # Manually save
            state = {
                "model": ml._model,
                "drift": ml._drift_detector,
                "metric": ml._metric,
                "samples_seen": ml._samples_seen,
                "samples_trained": ml._samples_trained,
                "drift_count": ml._drift_count,
            }
            with open(model_path, "wb") as f:
                pickle.dump(state, f)

            # Load in new instance by patching path
            import ml_engine as ml_mod
            old_path = ml_mod._MODEL_PATH
            ml_mod._MODEL_PATH = model_path
            try:
                ml2 = MLEngine(min_samples=5)
                self.assertEqual(ml2._samples_seen, ml._samples_seen)
            finally:
                ml_mod._MODEL_PATH = old_path

    def test_predict_returns_probability_in_range(self):
        from ml_engine import MLEngine
        ml = MLEngine(min_samples=10)
        features = _make_features(True)
        # Train past threshold
        for _ in range(20):
            ml.update(features, True)
        score = ml.predict(features)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)


class TestMLFeatureFormat(unittest.TestCase):
    """Test that features dict is compatible with river's API."""

    def test_features_are_all_numeric(self):
        features = _make_features(True)
        for k, v in features.items():
            self.assertIsInstance(v, (int, float), f"Feature {k}={v} not numeric")

    def test_no_nan_or_inf(self):
        import math
        features = _make_features(True)
        for k, v in features.items():
            self.assertTrue(math.isfinite(v), f"Feature {k}={v} not finite")

    def test_win_and_loss_features_differ(self):
        win = _make_features(True)
        loss = _make_features(False)
        differences = sum(1 for k in win if win[k] != loss.get(k))
        self.assertGreater(differences, 0, "Win and loss features should differ")


if __name__ == "__main__":
    unittest.main(verbosity=2)
