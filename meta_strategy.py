"""
KAVACH-07 — Meta-Strategy Engine
Runs all strategy modules per symbol, aggregates signals with weighted consensus,
applies regime-aware modifiers, and emits a MetaSignal (or NEUTRAL).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from ..strategies.base import MetaSignal, Signal
from ..strategies import ALL_STRATEGY_CLASSES, build_strategies_for_symbol
from ..strategies.regime_filter import (
    REGIME_TRENDING, REGIME_RANGING, REGIME_VOLATILE, REGIME_UNDEFINED,
)

logger = logging.getLogger(__name__)


class MetaStrategy:
    """Aggregates signals from all strategy modules into a final MetaSignal.

    For each active symbol:
    1. Run RegimeFilter first → read regime metadata.
    2. Apply regime-based weight modifiers.
    3. Run all other strategies concurrently.
    4. Compute weighted confidence sum for LONG vs SHORT.
    5. Emit MetaSignal if directional consensus > threshold.
    """

    def __init__(self, config: dict, symbols: List[str]) -> None:
        self._cfg         = config
        self._mcfg        = config.get("meta_strategy", {})
        self._strategy_cfg = config.get("strategies", {})
        self._symbols     = symbols
        self._consensus_threshold = float(self._mcfg.get("consensus_threshold_percent", 60.0))
        self._min_strategies      = int(self._mcfg.get("min_contributing_strategies", 2))
        self._regime_mods         = self._mcfg.get("regime_weight_modifiers", {})

        # Build strategy instances per symbol
        self._strategies: Dict[str, list] = {
            sym: build_strategies_for_symbol(config, sym)
            for sym in symbols
        }
        total = sum(len(v) for v in self._strategies.values())
        logger.info(
            "MetaStrategy: %d symbols × ~%d strategies = %d instances",
            len(symbols), total // max(len(symbols), 1), total,
        )

    # ─────────────────────────────────────────────────────────────────────
    # Main entry point
    # ─────────────────────────────────────────────────────────────────────

    async def analyze(
        self, data_context: Dict[str, Any]
    ) -> List[MetaSignal]:
        """Run analysis for all symbols and return list of MetaSignals."""
        tasks = [
            self._analyze_symbol(sym, data_context)
            for sym in self._symbols
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        signals: List[MetaSignal] = []
        for sym, result in zip(self._symbols, results):
            if isinstance(result, Exception):
                logger.error("MetaStrategy.analyze(%s) exception: %s", sym, result, exc_info=True)
                continue
            if result is not None and result.side != "NEUTRAL":
                signals.append(result)
        return signals

    # ─────────────────────────────────────────────────────────────────────
    # Per-symbol analysis
    # ─────────────────────────────────────────────────────────────────────

    async def _analyze_symbol(
        self, symbol: str, data_context: Dict[str, Any]
    ) -> MetaSignal:
        strategy_list = self._strategies.get(symbol, [])
        if not strategy_list:
            return self._neutral_meta(symbol, "No strategies configured")

        # ── Step 1: Run RegimeFilter first (synchronously) ───────────────
        regime           = REGIME_UNDEFINED
        regime_signal    = None
        regime_strategy  = next((s for s in strategy_list if s.__class__.__name__ == "RegimeFilter"), None)
        if regime_strategy:
            try:
                regime_signal = await regime_strategy.generate_signal(data_context)
                regime = regime_signal.metadata.get("regime", REGIME_UNDEFINED)
            except Exception as exc:
                logger.warning("RegimeFilter(%s) failed: %s", symbol, exc)

        # ── Step 2: Build effective weights (base × regime modifier) ─────
        effective_weights = self._build_effective_weights(regime)

        # ── Step 3: Run all non-regime strategies concurrently ───────────
        non_regime = [s for s in strategy_list if s.__class__.__name__ != "RegimeFilter"]
        raw_signals: List[Signal] = []
        if non_regime:
            tasks = [s.generate_signal(data_context) for s in non_regime]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for strat, res in zip(non_regime, results):
                if isinstance(res, Exception):
                    logger.debug("%s(%s) exception: %s", strat.strategy_name, symbol, res)
                    continue
                raw_signals.append(res)

        # ── Step 4: Aggregate ─────────────────────────────────────────────
        return self._aggregate(symbol, raw_signals, effective_weights, regime)

    # ─────────────────────────────────────────────────────────────────────
    # Aggregation logic
    # ─────────────────────────────────────────────────────────────────────

    def _aggregate(
        self,
        symbol: str,
        signals: List[Signal],
        weights: Dict[str, float],
        regime: str,
    ) -> MetaSignal:
        long_score     = 0.0
        short_score    = 0.0
        total_weight   = 0.0
        long_signals:  List[Signal] = []
        short_signals: List[Signal] = []

        for sig in signals:
            if sig.side == "NEUTRAL" or sig.confidence <= 0:
                continue
            strat_name = sig.metadata.get("strategies_fired", [""])[0] if sig.metadata.get("strategies_fired") else ""
            # Derive config key
            key    = _to_snake(strat_name)
            weight = float(weights.get(key, self._get_base_weight(key)))
            contribution = sig.confidence * weight

            if sig.side == "LONG":
                long_score += contribution
                long_signals.append(sig)
            elif sig.side == "SHORT":
                short_score += contribution
                short_signals.append(sig)
            total_weight += weight

        if total_weight < 1e-10:
            return self._neutral_meta(symbol, "No weighted signals")

        total_score = long_score + short_score
        if total_score < 1e-10:
            return self._neutral_meta(symbol, "All signals neutral")

        long_pct  = long_score  / total_score * 100.0
        short_pct = short_score / total_score * 100.0

        # Check consensus threshold
        if long_pct >= self._consensus_threshold and len(long_signals) >= self._min_strategies:
            return self._build_meta_signal(
                symbol, "LONG", long_signals, long_pct, long_score / total_weight, regime
            )
        elif short_pct >= self._consensus_threshold and len(short_signals) >= self._min_strategies:
            return self._build_meta_signal(
                symbol, "SHORT", short_signals, short_pct, short_score / total_weight, regime
            )
        else:
            return self._neutral_meta(
                symbol,
                f"No consensus: LONG={long_pct:.1f}% SHORT={short_pct:.1f}% "
                f"(need ≥{self._consensus_threshold:.0f}% with ≥{self._min_strategies} strategies)",
            )

    def _build_meta_signal(
        self,
        symbol: str,
        side: str,
        contributing: List[Signal],
        consensus_pct: float,
        agg_confidence: float,
        regime: str,
    ) -> MetaSignal:
        """Build MetaSignal from the list of contributing directional signals."""
        # Use highest-confidence signal for entry/SL/TP
        best = max(contributing, key=lambda s: s.confidence)

        # Weighted average entry (fallback to best)
        total_w = sum(s.confidence for s in contributing)
        if total_w > 0:
            w_entry = sum(s.entry * s.confidence for s in contributing if s.entry > 0) / max(
                sum(s.confidence for s in contributing if s.entry > 0), 1e-10
            )
        else:
            w_entry = best.entry

        entry = w_entry if w_entry > 0 else best.entry

        # Rationale
        strat_names  = [
            s.metadata.get("strategies_fired", ["?"])[0] for s in contributing
        ]
        rationale    = (
            f"[{side}] Regime={regime} | Consensus={consensus_pct:.1f}% | "
            f"Conf={agg_confidence:.1f} | "
            f"Strategies: {', '.join(strat_names[:5])} | "
            f"Best: {best.rationale[:120]}"
        )

        return MetaSignal(
            symbol=symbol,
            side=side,
            confidence=round(min(99.0, agg_confidence), 2),
            entry=round(entry, 8),
            stop_loss=round(best.stop_loss, 8),
            take_profit=round(best.take_profit, 8),
            rationale=rationale,
            strategies_fired=strat_names,
            regime=regime,
        )

    def _neutral_meta(self, symbol: str, reason: str) -> MetaSignal:
        return MetaSignal(
            symbol=symbol, side="NEUTRAL", confidence=0.0,
            entry=0.0, stop_loss=0.0, take_profit=0.0,
            rationale=reason, regime=REGIME_UNDEFINED,
        )

    # ─────────────────────────────────────────────────────────────────────
    # Weight helpers
    # ─────────────────────────────────────────────────────────────────────

    def _build_effective_weights(self, regime: str) -> Dict[str, float]:
        """Base weights × regime modifiers."""
        regime_key = regime.lower()
        mods       = self._regime_mods.get(regime_key, {})
        weights    = {}
        for cls in ALL_STRATEGY_CLASSES:
            key    = _to_snake(cls.__name__)
            base_w = self._get_base_weight(key)
            mod    = float(mods.get(key, 1.0))
            weights[key] = base_w * mod
        return weights

    def _get_base_weight(self, key: str) -> float:
        return float(self._strategy_cfg.get(key, {}).get("weight", 1.0))


def _to_snake(name: str) -> str:
    if not name:
        return ""
    result = [name[0].lower()]
    for ch in name[1:]:
        if ch.isupper():
            result.append("_")
            result.append(ch.lower())
        else:
            result.append(ch)
    return "".join(result)
