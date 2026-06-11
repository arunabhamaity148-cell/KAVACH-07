"""
KAVACH-07 — Strategy Base
Abstract base class all strategies must implement.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from models import DataSnapshot, Signal


class BaseStrategy(ABC):
    """
    Every strategy implements scan() and get_required_data().
    scan() returns a Signal or None.
    scan() must never raise — return None on any error.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy identifier string, e.g. 'LIQUIDATION_FADE'."""
        ...

    @property
    def min_risk_pct(self) -> float:
        return 0.003

    @property
    def max_risk_pct(self) -> float:
        return 0.005

    @abstractmethod
    def get_required_data(self) -> List[str]:
        """
        Return list of DataSnapshot fields required.
        Used by SignalEngine to skip strategies when data is missing.
        e.g. ['candles_5m', 'cvd_z_score', 'oi_change_1h']
        """
        ...

    @abstractmethod
    def scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        """
        Evaluate strategy for a symbol with provided data snapshot.
        Returns Signal or None (no setup found).
        Must be side-effect-free (reads only, no state mutation).
        Must never raise exceptions.
        """
        ...

    def _clamp_confidence(self, v: float, lo: float = 0.50, hi: float = 0.85) -> float:
        return max(lo, min(hi, v))

    def _safe_pct(self, a: float, b: float) -> float:
        """(a - b) / b, guarded against divide-by-zero."""
        if abs(b) < 1e-10:
            return 0.0
        return (a - b) / b
