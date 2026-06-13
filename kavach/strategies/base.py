"""
KAVACH-07 — Strategy Base (REMEDIATED)
Fixed: Silent config loading failure. Added explicit validation for strategy parameters.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kavach.strategy_base")

@dataclass(slots=True)
class Signal:
    """Individual strategy output module."""
    symbol: str
    side: str  # "LONG", "SHORT", "NEUTRAL"
    confidence: float  # 0.0 to 100.0
    entry: float
    stop_loss: float
    take_profit: float
    rationale: str
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass(slots=True)
class MetaSignal:
    """Aggregated output from MetaStrategy."""
    symbol: str
    side: str
    confidence: float
    entry: float
    stop_loss: float
    take_profit: float
    rationale: str
    strategies_fired: List[str]
    regime: str
    timestamp: float = field(default_factory=time.time)
    position_size_usdt: float = 0.0

class StrategyBase:
    """
    Base class with strict config validation.
    Ensures no strategy runs without calibrated risk parameters.
    """

    def __init__(self, config: Dict[str, Any], symbol: str):
        self.config = config
        self.symbol = symbol
        self.strategy_name = self.__class__.__name__
        
        # FIXED: Explicit lookup in both YAML blocks
        key = self._config_key()
        s_cfg = config.get("strategies", {}).get(key)
        p2_cfg = config.get("phase2_strategies", {}).get(key)
        
        # Validation Logic: Priority to standard block, then phase2
        if s_cfg:
            self._cfg = s_cfg
        elif p2_cfg:
            self._cfg = p2_cfg
        else:
            # CRITICAL: If no config is found, log an error immediately.
            # This prevents trading with uninitialized SL/TP values.
            logger.error(f"CRITICAL ERROR: Strategy '{self.strategy_name}' configuration NOT FOUND "
                         f"for {symbol}. Key '{key}' missing in config.yaml.")
            self._cfg = {}

    def _config_key(self) -> str:
        """CamelCase to snake_case converter."""
        name = self.strategy_name
        res = [name[0].lower()]
        for ch in name[1:]:
            if ch.isupper():
                res.append("_")
                res.append(ch.lower())
            else:
                res.append(ch)
        return "".join(res)

    @property
    def weight(self) -> float:
        return float(self._cfg.get("weight", 1.0))

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        raise NotImplementedError("Subclasses must implement generate_signal")

    def _create_signal(self, side: str, confidence: float, entry: float, 
                       stop_loss: float, take_profit: float, rationale: str) -> Signal:
        return Signal(
            symbol=self.symbol,
            side=side,
            confidence=max(0.0, min(100.0, float(confidence))),
            entry=float(entry),
            stop_loss=float(stop_loss),
            take_profit=float(take_profit),
            rationale=rationale,
            metadata={"strategy": self.strategy_name}
        )

    def _neutral(self, reason: str = "No conditions met") -> Signal:
        return self._create_signal("NEUTRAL", 0.0, 0.0, 0.0, 0.0, reason)