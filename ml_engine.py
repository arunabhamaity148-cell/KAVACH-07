"""
KAVACH-07 — ML Engine
Online learning with river. Predicts signal success probability.
Detects concept drift with ADWIN and resets model when detected.
Zero overfitting: adapts continuously, predicts 0.5 until trained.
"""
from __future__ import annotations

import json
import os
import pickle
import traceback
from typing import Optional

from utils import get_logger

logger = get_logger(__name__)

_MODEL_PATH = "ml_model.pkl"
_MIN_SAMPLES = 100   # Predict 0.5 until this many labelled samples seen


class MLEngine:
    """
    Online learning signal scorer using river library.
    - Features: market snapshot + signal metadata
    - Label: True if trade closed with PnL > 0
    - Model: StandardScaler → LogisticRegression
    - Drift: ADWIN detector — resets model on detected drift
    """

    def __init__(self, min_samples: int = _MIN_SAMPLES):
        self._min_samples = min_samples
        self._samples_seen = 0
        self._samples_trained = 0
        self._drift_count = 0

        self._model = None
        self._drift_detector = None
        self._metric = None

        self._load_or_init()

    def _load_or_init(self) -> None:
        """Load persisted model or init fresh."""
        if os.path.exists(_MODEL_PATH):
            try:
                with open(_MODEL_PATH, "rb") as f:
                    state = pickle.load(f)
                self._model = state["model"]
                self._drift_detector = state["drift"]
                self._metric = state["metric"]
                self._samples_seen = state.get("samples_seen", 0)
                self._samples_trained = state.get("samples_trained", 0)
                self._drift_count = state.get("drift_count", 0)
                logger.info(
                    f"ML model loaded: {self._samples_seen} samples, "
                    f"{self._drift_count} drifts detected"
                )
                return
            except Exception as e:
                logger.warning(f"Failed to load ML model: {e} — initialising fresh")

        self._init_fresh()

    def _init_fresh(self) -> None:
        """Initialise a new model pipeline."""
        try:
            from river import compose, preprocessing, linear_model, metrics, drift

            self._model = compose.Pipeline(
                preprocessing.StandardScaler(),
                linear_model.LogisticRegression(
                    optimizer=None,      # uses Adam by default
                    l2=0.01,
                )
            )
            self._drift_detector = drift.ADWIN(delta=0.002)
            self._metric = metrics.ROCAUC()
            logger.info("ML model initialised fresh (river)")
        except ImportError:
            logger.warning("river library not installed — ML scoring disabled")
            self._model = None

    def predict(self, features: dict) -> float:
        """
        Predict P(win) for a trade setup.
        Returns 0.5 if untrained.
        """
        if self._model is None or self._samples_seen < self._min_samples:
            return 0.5

        try:
            proba = self._model.predict_proba_one(features)
            return float(proba.get(True, 0.5))
        except Exception:
            logger.debug(f"ML predict error: {traceback.format_exc()}")
            return 0.5

    def update(self, features: dict, win: bool) -> None:
        """
        Update model with a closed trade outcome.
        win=True if trade was profitable.
        """
        self._samples_seen += 1

        if self._model is None:
            return

        # Drift detection (track error)
        if self._samples_seen >= self._min_samples:
            try:
                predicted_prob = self._model.predict_proba_one(features).get(True, 0.5)
                prediction = predicted_prob > 0.5
                error = 0.0 if (prediction == win) else 1.0
                self._drift_detector.update(error)

                if self._drift_detector.drift_detected:
                    self._drift_count += 1
                    logger.warning(
                        f"ML drift detected (#{self._drift_count}) — resetting model"
                    )
                    self._init_fresh()
                    self._samples_trained = 0
                    self._save()
                    return

                # Update model
                self._model.learn_one(features, win)
                self._metric.update(win, predicted_prob)
                self._samples_trained += 1

                # Periodic save
                if self._samples_trained % 50 == 0:
                    self._save()
                    logger.debug(
                        f"ML updated: {self._samples_trained} samples, "
                        f"ROC-AUC: {self._metric.get():.3f}"
                    )
            except Exception:
                logger.debug(f"ML update error: {traceback.format_exc()}")
        else:
            # Pre-training: still accumulate (learn without predicting)
            try:
                self._model.learn_one(features, win)
                self._samples_trained += 1
            except Exception:
                pass

    def _save(self) -> None:
        """Persist model to disk."""
        if self._model is None:
            return
        try:
            state = {
                "model": self._model,
                "drift": self._drift_detector,
                "metric": self._metric,
                "samples_seen": self._samples_seen,
                "samples_trained": self._samples_trained,
                "drift_count": self._drift_count,
            }
            with open(_MODEL_PATH + ".tmp", "wb") as f:
                pickle.dump(state, f)
            os.replace(_MODEL_PATH + ".tmp", _MODEL_PATH)
        except Exception as e:
            logger.warning(f"ML model save failed: {e}")

    @property
    def is_trained(self) -> bool:
        return self._samples_seen >= self._min_samples

    @property
    def stats(self) -> dict:
        roc = 0.0
        if self._metric:
            try:
                roc = self._metric.get()
            except Exception:
                pass
        return {
            "samples_seen": self._samples_seen,
            "samples_trained": self._samples_trained,
            "drift_detections": self._drift_count,
            "is_trained": self.is_trained,
            "roc_auc": round(roc, 4),
            "model_available": self._model is not None,
        }

    @property
    def drift_detected(self) -> bool:
        if self._drift_detector is None:
            return False
        try:
            return bool(self._drift_detector.drift_detected)
        except Exception:
            return False
