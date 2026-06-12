"""
KAVACH-07 — Signal Engine
Runs all 10 strategies on all pairs every SCAN_INTERVAL seconds.
Applies pre-filters, ML scoring, deduplication.
Returns best signal per pair per scan.
"""
from __future__ import annotations

import asyncio
import time
import traceback
from typing import Dict, List, Optional

from config import Config
from data_engine import DataEngine
from database import Database
from ml_engine import MLEngine
from models import DataSnapshot, Signal
from strategies import (
    LiquidationFade, FundingSqueeze, OBImbalance, LiquiditySweep,
    VPNode, OIBreakout, BasisReversion, RegimeFilter, SocialFade, ExchangeArb,
)
from strategies.base import BaseStrategy
from utils import get_logger

logger = get_logger(__name__)

# Pre-filter thresholds
_MAX_SPREAD_PCT  = 0.001    # 0.1%
_MIN_VOLUME_RATIO = 1.2
_MIN_ATR_PCT     = 0.001    # 0.1% (skip dead markets)
_MIN_CONFIDENCE  = 0.55     # After ML adjustment
_SIGNAL_COOLDOWN_MS = 180_000  # 3-minute cooldown per symbol+strategy

# Strategy name → class mapping
_STRATEGY_MAP = {
    "LIQUIDATION_FADE": LiquidationFade,
    "FUNDING_SQUEEZE":  FundingSqueeze,
    "OB_IMBALANCE":     OBImbalance,
    "LIQUIDITY_SWEEP":  LiquiditySweep,
    "VP_NODE":          VPNode,
    "OI_BREAKOUT":      OIBreakout,
    "BASIS_REVERSION":  BasisReversion,
    "REGIME_FILTER":    RegimeFilter,
    "SOCIAL_FADE":      SocialFade,
    "EXCHANGE_ARB":     ExchangeArb,
}


class SignalEngine:

    def __init__(self, config: Config, data_engine: DataEngine,
                 db: Database, ml_engine: MLEngine):
        self._cfg = config
        self._de = data_engine
        self._db = db
        self._ml = ml_engine

        # Instantiate all enabled strategies
        self._strategies: List[BaseStrategy] = []
        self._regime_filter = RegimeFilter()

        for name in config.STRATEGIES:
            cls = _STRATEGY_MAP.get(name)
            if cls and name != "REGIME_FILTER":
                self._strategies.append(cls())
                logger.info(f"Strategy loaded: {name}")
            elif name == "REGIME_FILTER":
                self._strategies.append(self._regime_filter)

        # Track duplicate signals in memory for speed
        self._recent_signals: Dict[str, float] = {}  # "symbol:strategy" → timestamp

        self._paused = False
        self._total_scans = 0
        self._total_signals = 0

    # ─── Main scan loop ──────────────────────────────────────

    async def run_scan(self) -> List[Signal]:
        """
        Single scan across all pairs and strategies.
        Returns list of approved signals (0 or more).
        """
        if self._paused:
            return []

        scan_start = time.monotonic()
        self._total_scans += 1

        # Build all snapshots
        snapshots: List[DataSnapshot] = []
        for sym in self._cfg.BASE_PAIRS:
            snap = self._de.get_snapshot(sym)
            if snap:
                snapshots.append(snap)

        if not snapshots:
            logger.warning("No snapshots available for scan")
            return []

        # Update regime (uses all snapshots)
        regime = self._regime_filter.compute_regime(snapshots)
        self._de.set_regime(regime)

        # Collect signals
        approved: List[Signal] = []

        for snap in snapshots:
            try:
                sym_signals = await self._scan_symbol(snap, regime)
                approved.extend(sym_signals)
            except Exception:
                logger.error(f"Scan error for {snap.symbol}:\n{traceback.format_exc()}")

        elapsed = time.monotonic() - scan_start
        if approved:
            logger.info(
                f"Scan #{self._total_scans}: {len(snapshots)} pairs, "
                f"{len(approved)} signals in {elapsed:.2f}s"
            )
        else:
            logger.debug(
                f"Scan #{self._total_scans}: {len(snapshots)} pairs, "
                f"0 signals in {elapsed:.2f}s"
            )

        self._total_signals += len(approved)
        return approved

    async def _scan_symbol(self, data: DataSnapshot, regime) -> List[Signal]:
        """Run all strategies on one symbol, apply filters and ML."""
        symbol = data.symbol
        candidates: List[Signal] = []

        # ── Pre-filters ───────────────────────────────────────
        filter_results = {
            "spread": data.spread_pct < _MAX_SPREAD_PCT,
            "volume": data.volume_ratio >= _MIN_VOLUME_RATIO or data.volume_ratio == 1.0,
            "atr":    self._atr_pct(data) >= _MIN_ATR_PCT,
        }

        if not all(filter_results.values()):
            logger.debug(
                f"{symbol} pre-filter failed: "
                + ", ".join(k for k, v in filter_results.items() if not v)
            )
            return []

        # ── Run each strategy ─────────────────────────────────
        for strategy in self._strategies:
            if strategy.name == "REGIME_FILTER":
                continue

            # Check if required data is present
            if not self._has_required_data(data, strategy):
                continue

            try:
                signal = strategy.scan(symbol, data)
            except Exception:
                logger.debug(f"{strategy.name}[{symbol}] raised: {traceback.format_exc()}")
                continue

            if signal is None:
                continue

            # ── Duplicate check ───────────────────────────────
            key = f"{symbol}:{strategy.name}"
            last_ts = self._recent_signals.get(key, 0.0)
            if time.time() - last_ts < _SIGNAL_COOLDOWN_MS / 1000:
                logger.debug(f"Duplicate signal suppressed: {key}")
                continue

            # DB-level duplicate check (cross-restart safety)
            if await self._db.signal_exists_recently(symbol, strategy.name,
                                                      _SIGNAL_COOLDOWN_MS):
                continue

            # ── Validate signal levels ────────────────────────
            if not self._validate_signal(signal):
                continue

            # ── Apply regime multiplier to risk ───────────────
            signal.risk_pct *= regime.position_multiplier

            # ── ML scoring ────────────────────────────────────
            features = self._build_features(data, signal)
            ml_score = self._ml.predict(features)
            signal.ml_score = ml_score

            # Blend strategy confidence with ML score
            if self._ml.is_trained:
                # Once trained, ML score influences confidence
                signal.confidence = 0.6 * signal.confidence + 0.4 * ml_score
            # (else: keep raw strategy confidence until trained)

            # ── Final confidence gate ─────────────────────────
            if signal.confidence < _MIN_CONFIDENCE:
                logger.debug(
                    f"{strategy.name}[{symbol}] confidence too low: "
                    f"{signal.confidence:.3f}"
                )
                continue

            # ── Regime funding filter ─────────────────────────
            # Skip extremely high-funding pairs unless strategy is funding-based
            if abs(data.funding_rate) > 0.0015 and \
               strategy.name not in ("FUNDING_SQUEEZE", "BASIS_REVERSION"):
                if data.funding_percentile > 95 or data.funding_percentile < 5:
                    logger.debug(f"{symbol} extreme funding, non-funding strategy skipped")
                    continue

            signal.filters_passed = filter_results
            candidates.append(signal)

        # ── Pick best signal per symbol (highest confidence) ──
        if not candidates:
            return []

        best = max(candidates, key=lambda s: s.confidence)

        # Record as recent
        self._recent_signals[f"{best.symbol}:{best.strategy}"] = time.time()

        # Save to DB
        await self._db.insert_signal(best)

        return [best]

    # ─── Helpers ─────────────────────────────────────────────

    def _atr_pct(self, data: DataSnapshot) -> float:
        if data.mid_price < 1e-10:
            return 0.0
        atr = data.atr_5m or data.atr_1m or data.atr_1h
        return atr / data.mid_price

    def _has_required_data(self, data: DataSnapshot, strategy: BaseStrategy) -> bool:
        for field in strategy.get_required_data():
            val = getattr(data, field, None)
            if val is None:
                return False
            if isinstance(val, (list,)) and len(val) == 0 and field.startswith("candles"):
                return False
        return True

    def _validate_signal(self, sig: Signal) -> bool:
        """Basic sanity checks on signal levels."""
        if sig.entry_price < 1e-10:
            return False
        if sig.sl_price < 1e-10 or sig.tp1_price < 1e-10:
            return False

        # SL can't be on wrong side
        if sig.direction == "LONG" and sig.sl_price >= sig.entry_price:
            return False
        if sig.direction == "SHORT" and sig.sl_price <= sig.entry_price:
            return False

        # TP must be profitable
        if sig.direction == "LONG" and sig.tp1_price <= sig.entry_price:
            return False
        if sig.direction == "SHORT" and sig.tp1_price >= sig.entry_price:
            return False

        # SL distance not too large
        sl_dist_pct = abs(sig.entry_price - sig.sl_price) / sig.entry_price
        if sl_dist_pct > self._cfg.MAX_SL_PCT:
            return False

        # Minimum R:R of 1.2
        if sig.r_ratio < 1.2:
            return False

        return True

    def _build_features(self, data: DataSnapshot, sig: Signal) -> dict:
        """Build ML feature dict from snapshot and signal."""
        return {
            "price_change_5m":     self._safe_div(
                float(data.candles_5m[-1]["close"]) - float(data.candles_5m[-2]["close"])
                if len(data.candles_5m) >= 2 else 0.0,
                float(data.candles_5m[-2]["close"]) if len(data.candles_5m) >= 2 else 1.0,
            ),
            "price_change_1h":     self._safe_div(
                float(data.candles_1h[-1]["close"]) - float(data.candles_1h[-2]["close"])
                if len(data.candles_1h) >= 2 else 0.0,
                float(data.candles_1h[-2]["close"]) if len(data.candles_1h) >= 2 else 1.0,
            ),
            "atr_pct":             self._atr_pct(data),
            "ob_imbalance":        data.ob_imbalance,
            "spread_pct":          data.spread_pct,
            "cvd_z_score":         data.cvd_z_score,
            "cvd_slope_5m":        data.cvd_slope_5m,
            "funding_rate":        data.funding_rate,
            "funding_percentile":  data.funding_percentile / 100.0,
            "oi_change_1h":        data.oi_change_1h,
            "oi_change_4h":        data.oi_change_4h,
            "volume_ratio":        data.volume_ratio,
            "delta_direction":     float(data.delta_direction),
            "fear_greed":          data.fear_greed_index / 100.0,
            "confidence_raw":      sig.confidence,
            "r_ratio":             sig.r_ratio,
            "sl_distance_pct":     sig.sl_distance_pct,
            "is_long":             1.0 if sig.direction == "LONG" else 0.0,
        }

    @staticmethod
    def _safe_div(a: float, b: float) -> float:
        return a / b if abs(b) > 1e-10 else 0.0

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def stats(self) -> dict:
        return {
            "total_scans": self._total_scans,
            "total_signals": self._total_signals,
            "paused": self._paused,
        }
