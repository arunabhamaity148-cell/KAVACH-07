"""
KAVACH-07 — ML Engine
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import pickle
import time
import traceback
from typing import Dict, List, Optional

from config import Config
from models import Signal, TradeResult
from utils import get_logger

logger = get_logger(__name__)

_MODEL_PATH = "ml_model.pkl"
_MODEL_CHECKSUM_PATH = "ml_model.pkl.sha256"

class MLEngine:

    def __init__(self, config: Config):
        self._cfg = config
        self._model = None
        self._scaler = None
        self._samples_seen = 0
        self._samples_trained = 0
        self._drift_detector = None
        self._is_trained = False
        self._load_model()

    def _load_model(self) -> None:
        try:
            if os.path.exists(_MODEL_PATH) and os.path.exists(_MODEL_CHECKSUM_PATH):
                with open(_MODEL_CHECKSUM_PATH, "r") as f:
                    expected_checksum = f.read().strip()
                with open(_MODEL_PATH, "rb") as f:
                    actual_checksum = hashlib.sha256(f.read()).hexdigest()
                if actual_checksum != expected_checksum:
                    logger.warning("ML model checksum mismatch — starting fresh")
                    self._init_model()
                    return
                with open(_MODEL_PATH, "rb") as f:
                    state = pickle.load(f)
                self._model = state.get("model")
                self._scaler = state.get("scaler")
                self._samples_seen = state.get("samples_seen", 0)
                self._samples_trained = state.get("samples_trained", 0)
                self._is_trained = state.get("is_trained", False)
                logger.info(f"ML model loaded: {self._samples_trained} trained samples")
            else:
                self._init_model()
        except Exception as e:
            logger.error(f"ML model load error: {e}")
            self._init_model()

    def _init_model(self) -> None:
        try:
            from river import compose, preprocessing, tree, drift
            self._scaler = preprocessing.StandardScaler()
            self._model = compose.Pipeline(
                ("scale", self._scaler),
                ("classifier", tree.HoeffdingAdaptiveTreeClassifier(
                    grace_period=50,
                    split_confidence=1e-5,
                    drift_detector=drift.ADWIN(delta=0.002),
                )),
            )
            self._drift_detector = drift.ADWIN(delta=0.001)
            logger.info("ML model initialized fresh")
        except ImportError as e:
            logger.error(f"river import failed: {e}")
            self._model = None

    async def start(self) -> None:
        logger.info("MLEngine started")

    async def stop(self) -> None:
        self._save()
        logger.info("MLEngine stopped")

    def predict(self, features: Dict[str, float]) -> float:
        if not self._is_trained or self._model is None:
            return 0.5
        try:
            proba = self._model.predict_proba_one(features)
            return proba.get(True, 0.5)
        except Exception as e:
            logger.warning(f"ML prediction error: {e}")
            return 0.5

    def update(self, features: Dict[str, float], result: TradeResult) -> None:
        if self._model is None:
            return
        win = result.pnl > 0
        self._samples_seen += 1

        if self._is_trained:
            try:
                self._model.learn_one(features, win)
                self._samples_trained += 1
            except Exception:
                logger.warning(f"ML online learn failed:\n{traceback.format_exc()}")
            try:
                prediction = self._model.predict_one(features)
                if prediction is not None:
                    self._drift_detector.update(int(prediction == win))
                    if self._drift_detector.drift_detected:
                        logger.warning("ML drift detected — model may need retraining")
            except Exception:
                pass
        else:
            try:
                self._model.learn_one(features, win)
                self._samples_trained += 1
            except Exception:
                logger.warning(f"ML pre-training learn failed:\n{traceback.format_exc()}")
                self._samples_seen -= 1
                return
            if self._samples_trained >= 100:
                self._is_trained = True
                logger.info(f"ML model trained on {self._samples_trained} samples")

    def _save(self) -> None:
        if self._model is None:
            return
        try:
            state = {
                "model": self._model,
                "scaler": self._scaler,
                "samples_seen": self._samples_seen,
                "samples_trained": self._samples_trained,
                "is_trained": self._is_trained,
            }
            tmp_path = _MODEL_PATH + ".tmp"
            with open(tmp_path, "wb") as f:
                pickle.dump(state, f)
            with open(tmp_path, "rb") as f:
                checksum = hashlib.sha256(f.read()).hexdigest()
            os.replace(tmp_path, _MODEL_PATH)
            with open(_MODEL_CHECKSUM_PATH, "w") as f:
                f.write(checksum)
            logger.info(f"ML model saved ({self._samples_trained} samples)")
        except Exception as e:
            logger.error(f"ML model save error: {e}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    @staticmethod
    def build_features(signal: Signal, data: "DataSnapshot") -> Dict[str, float]:
        return {
            "confidence": signal.confidence,
            "risk_pct": signal.risk_pct,
            "atr_pct": signal.atr / signal.entry_price if signal.entry_price > 0 else 0,
            "r_ratio": signal.r_ratio,
            "sl_distance_pct": signal.sl_distance_pct,
            "funding_percentile": data.funding_percentile,
            "oi_change_1h": data.oi_change_1h,
            "oi_change_4h": data.oi_change_4h,
            "cvd_z_score": data.cvd_z_score,
            "cvd_slope_5m": data.cvd_slope_5m,
            "ob_imbalance": data.ob_imbalance,
            "spread_pct": data.spread_pct,
            "volume_ratio": data.volume_ratio,
            "basis_pct": data.basis_pct,
            "hour": signal.timestamp.hour,
            "day_of_week": signal.timestamp.weekday(),
        }
