"""
KAVACH-07 — Strategy Base
Defines the Signal dataclass and abstract StrategyBase class.
All strategy modules must inherit from StrategyBase and implement generate_signal().
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(slots=True)
class Signal:
    """A directional trading signal from one strategy module."""

    symbol: str
    side: str                 # "LONG" | "SHORT" | "NEUTRAL"
    confidence: float         # 0–100 — higher = stronger conviction
    entry: float              # Suggested entry price
    stop_loss: float          # Stop-loss price
    take_profit: float        # Take-profit price
    rationale: str            # Human-readable explanation
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class MetaSignal:
    """Aggregated consensus signal produced by MetaStrategy."""

    symbol: str
    side: str
    confidence: float
    entry: float
    stop_loss: float
    take_profit: float
    rationale: str
    strategies_fired: list = field(default_factory=list)
    position_size_usdt: float = 0.0
    regime: str = "UNDEFINED"
    timestamp: float = field(default_factory=time.time)


class StrategyBase:
    """Abstract base for all KAVACH-07 strategy modules.

    Subclasses must implement ``generate_signal()``.

    Parameters
    ----------
    config:
        Full bot configuration dict (loaded from config.yaml).
    symbol:
        The trading pair this strategy instance is bound to.
    """

    def __init__(self, config: Dict[str, Any], symbol: str) -> None:
        self.config = config
        self.symbol = symbol
        self.strategy_name: str = self.__class__.__name__
        # Strategy-specific config section (or empty dict if not present)
        self._cfg: Dict[str, Any] = (
            config.get("strategies", {}).get(self._config_key(), {})
        )

    def _config_key(self) -> str:
        """Map class name to config.yaml strategy key (snake_case)."""
        # Convert CamelCase → snake_case
        name = self.strategy_name
        result = [name[0].lower()]
        for ch in name[1:]:
            if ch.isupper():
                result.append("_")
                result.append(ch.lower())
            else:
                result.append(ch)
        return "".join(result)

    def _neutral(self, reason: str = "No signal") -> Signal:
        """Return a NEUTRAL signal with zero confidence."""
        return Signal(
            symbol=self.symbol,
            side="NEUTRAL",
            confidence=0.0,
            entry=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            rationale=reason,
            metadata={"strategies_fired": []},
        )

    def _create_signal(
        self,
        symbol: str,
        side: str,
        confidence: float,
        entry: float,
        stop_loss: float,
        take_profit: float,
        rationale: str,
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> Signal:
        """Construct a Signal with standard metadata."""
        meta: Dict[str, Any] = {"strategies_fired": [self.strategy_name]}
        if extra_meta:
            meta.update(extra_meta)
        return Signal(
            symbol=symbol,
            side=side,
            confidence=min(max(confidence, 0.0), 100.0),
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            rationale=rationale,
            metadata=meta,
        )

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        """Override in each strategy. Must return a Signal."""
        raise NotImplementedError(
            f"{self.strategy_name}.generate_signal() must be implemented."
        )
