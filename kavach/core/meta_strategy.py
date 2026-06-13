"""
KAVACH-07 — Meta Strategy Engine
Aggregates signals from all strategy modules and calculates consensus.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Type

from kavach.strategies.base import Signal, MetaSignal, StrategyBase
from kavach.strategies import build_strategies_for_symbol

logger = logging.getLogger("kavach.meta_strategy")

class MetaStrategy:
    """
    Orchestrates strategy execution and applies weighted consensus logic.
    """

    def __init__(self, config: dict, symbols: List[str], order_book_manager: Any):
        self._cfg = config
        self._symbols = symbols
        self._ob_mgr = order_book_manager
        
        # Consensus Config
        m_cfg = config["meta_strategy"]
        self._threshold = float(m_cfg["consensus_threshold_percent"])
        self._min_strategies = int(m_cfg["min_contributing_strategies"])
        self._regime_mods = m_cfg["regime_weight_modifiers"]
        
        # Strategy instances per symbol
        self._strategies: Dict[str, List[StrategyBase]] = {
            s: build_strategies_for_symbol(config, s) for s in symbols
        }
        
        # Exception tracking for auto-disabling (Bug #30)
        # Key: (symbol, strategy_name), Value: consecutive_error_count
        self._error_counts: Dict[tuple, int] = defaultdict(int)
        self._disabled_strategies: Dict[str, set] = defaultdict(set)

        # External bias engines
        self._news_engine: Optional[Any] = None
        self._whale_engine: Optional[Any] = None

    def set_news_engine(self, engine: Any) -> None:
        self._news_engine = engine

    def set_whale_engine(self, engine: Any) -> None:
        self._whale_engine = engine

    async def analyze(self, data_context: Dict[str, Any]) -> List[MetaSignal]:
        """
        Main analysis loop. Analyzes all symbols concurrently.
        """
        # Inject OrderBookManager into data context for liquidity strategies
        data_context["order_book_manager"] = self._ob_mgr
        
        tasks = [self._analyze_symbol(s, data_context) for s in self._symbols]
        results = await asyncio.gather(*tasks)
        
        # Filter out None results (where no consensus was reached)
        return [r for r in results if r is not None]

    async def _analyze_symbol(self, symbol: str, data_ctx: Dict[str, Any]) -> Optional[MetaSignal]:
        """
        Executes consensus logic for a single symbol.
        """
        strategies = self._strategies.get(symbol, [])
        if not strategies:
            return None

        # 1. RegimeFilter FIRST (Zero weight, determines modifiers)
        regime = "UNDEFINED"
        regime_filter = next((s for s in strategies if s.strategy_name == "RegimeFilter"), None)
        
        if regime_filter:
            try:
                reg_sig = await regime_filter.generate_signal(data_ctx)
                regime = reg_sig.metadata.get("regime", "UNDEFINED")
            except Exception as e:
                logger.error("CRITICAL: RegimeFilter failed for %s: %s", symbol, e)

        # 2. Prepare remaining strategies
        active_strategies = [
            s for s in strategies 
            if s.strategy_name != "RegimeFilter" 
            and s.strategy_name not in self._disabled_strategies[symbol]
        ]
        
        if not active_strategies:
            return None

        # 3. Concurrent Execution
        results = await asyncio.gather(
            *[s.generate_signal(data_ctx) for s in active_strategies],
            return_exceptions=True
        )

        # 4. Process Results and Aggregate Consensus
        # We group by Side (LONG/SHORT)
        side_scores = {"LONG": 0.0, "SHORT": 0.0}
        side_counts = {"LONG": 0, "SHORT": 0}
        total_weight = 0.0
        
        contributing_signals = []

        for i, res in enumerate(results):
            strat = active_strategies[i]
            strat_key = (symbol, strat.strategy_name)

            # Exception Handling Logic (Bug #30)
            if isinstance(res, Exception):
                logger.critical("Strategy Error: %s on %s -> %s", strat.strategy_name, symbol, res)
                self._error_counts[strat_key] += 1
                if self._error_counts[strat_key] >= 3:
                    logger.error("AUTO-DISABLE: Strategy %s on %s due to repeated errors", strat.strategy_name, symbol)
                    self._disabled_strategies[symbol].add(strat.strategy_name)
                continue
            
            # Reset error count on success
            self._error_counts[strat_key] = 0

            # Skip Neutral
            if res.side == "NEUTRAL" or res.confidence <= 0:
                continue

            # Calculate Weighted Contribution
            # Confidence * Weight * Regime_Modifier
            base_weight = strat.weight
            regime_mod = self._regime_mods.get(regime.lower(), {}).get(strat._config_key(), 1.0)
            effective_weight = base_weight * regime_mod
            
            score = res.confidence * effective_weight
            
            side_scores[res.side] += score
            side_counts[res.side] += 1
            total_weight += effective_weight
            contributing_signals.append(res)

        if total_weight <= 0:
            return None

        # 5. Consensus Calculation
        # Confidence = Weighted_Sum / Total_Weight
        long_conf = (side_scores["LONG"] / total_weight) if total_weight > 0 else 0
        short_conf = (side_scores["SHORT"] / total_weight) if total_weight > 0 else 0
        
        # Determine winning side based on consensus threshold
        winner_side = None
        final_confidence = 0.0
        
        # Logic: We take the side that meets the threshold and has more contributors
        if long_conf >= self._threshold and side_counts["LONG"] >= self._min_strategies:
            winner_side = "LONG"
            final_confidence = long_conf
        elif short_conf >= self._threshold and side_counts["SHORT"] >= self._min_strategies:
            winner_side = "SHORT"
            final_confidence = short_conf

        if not winner_side:
            return None

        # 6. Build MetaSignal
        # Extract best entry/sl/tp from the most confident contributing strategy
        best_sig = max([s for s in contributing_signals if s.side == winner_side], key=lambda x: x.confidence)
        
        # Aggregate Rationales
        rationales = [s.rationale for s in contributing_signals if s.side == winner_side]
        
        # Sentiment-Direction Guard check is performed in RiskManager/AlertManager
        # but we embed news/whale bias into metadata here for visibility
        news_bias = self._news_engine.get_status() if self._news_engine else None
        whale_bias = self._whale_engine.get_bias() if self._whale_engine else None

        return MetaSignal(
            symbol=symbol,
            side=winner_side,
            confidence=round(final_confidence, 2),
            entry=best_sig.entry,
            stop_loss=best_sig.stop_loss,
            take_profit=best_sig.take_profit,
            rationale=" | ".join(rationales[:3]), # Top 3 rationales
            strategies_fired=[s.metadata["strategy"] for s in contributing_signals if s.side == winner_side],
            regime=regime,
            timestamp=time.time()
        )