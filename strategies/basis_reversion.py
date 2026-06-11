"""
KAVACH-07 — Strategy 7: BASIS_REVERSION
Perp detaches from spot index; stat-arb expects reversion.

Setup:
  • Perp-spot basis z-score > 2.5 (perp premium too high → SHORT)
  • Or basis z-score < -2.5 (perp discount too deep → LONG)
  • Trade delta confirms exhaustion of the dominant side
"""
from __future__ import annotations

from typing import List, Optional

from models import DataSnapshot, Signal
from strategies.base import BaseStrategy
from utils import get_logger

logger = get_logger(__name__)

_BASIS_Z_THRESHOLD = 2.5   # Standard deviations
_MIN_FUNDING_HIST  = 50    # Need enough history


class BasisReversion(BaseStrategy):

    @property
    def name(self) -> str:
        return "BASIS_REVERSION"

    def get_required_data(self) -> List[str]:
        return ["mark_price", "index_price", "funding_history", "delta_direction", "atr_5m"]

    def scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        try:
            return self._scan(symbol, data)
        except Exception as e:
            logger.debug(f"BasisReversion[{symbol}] error: {e}")
            return None

    def _scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        mark  = data.mark_price
        index = data.index_price

        if mark < 1e-10 or index < 1e-10:
            return None

        if len(data.funding_history) < _MIN_FUNDING_HIST:
            return None

        atr = data.atr_5m
        if atr < 1e-10:
            return None

        # ── Basis = (mark - index) / index ───────────────────
        basis = (mark - index) / index
        current = mark   # Trade the perp (mark price)

        # ── Historical basis from funding history (proxy) ─────
        # Funding rate is highly correlated with perp-spot basis
        # Use funding history to compute z-score of current basis
        import numpy as np
        hist = data.funding_history
        arr = np.array(hist)
        mean = arr.mean()
        std  = arr.std()

        if std < 1e-10:
            return None

        # Funding rate as basis proxy (same sign/direction)
        current_rate = data.funding_rate
        z_score = (current_rate - mean) / std

        if abs(z_score) < _BASIS_Z_THRESHOLD:
            return None

        # ── Direction ─────────────────────────────────────────
        if z_score > _BASIS_Z_THRESHOLD:
            # Perp at premium → longs paying high funding → SHORT the perp
            direction = "SHORT"
            if data.delta_direction == 1:
                return None   # Delta still bullish — no exhaustion yet
        else:
            # Perp at discount → shorts paying high funding → LONG the perp
            direction = "LONG"
            if data.delta_direction == -1:
                return None

        # ── Price levels (tight — basis reverts quickly) ──────
        if direction == "SHORT":
            entry = current
            sl    = entry + 0.8 * atr
            tp1   = entry - 1.5 * (sl - entry)   # ~1.9R
        else:
            entry = current
            sl    = entry - 0.8 * atr
            tp1   = entry + 1.5 * (entry - sl)

        # ── Confidence ────────────────────────────────────────
        base = 0.52
        base += min(0.08, (abs(z_score) - 2.5) * 0.03)
        base += 0.03 if abs(basis) > 0.003 else 0.0  # >0.3% basis adds conviction
        confidence = self._clamp_confidence(base)

        rationale = (
            f"Funding z-score: {z_score:+.2f}σ "
            f"({'perp premium' if direction=='SHORT' else 'perp discount'})\n"
            f"Spot: {index:.2f}\n"
            f"Perp: {mark:.2f}\n"
            f"Basis: {basis*100:+.3f}%\n"
            f"Delta exhaustion confirmed"
        )

        return Signal(
            symbol=symbol,
            strategy=self.name,
            direction=direction,
            confidence=confidence,
            entry_type="LIMIT",
            entry_price=round(entry, 6),
            sl_price=round(sl, 6),
            tp1_price=round(tp1, 6),
            risk_pct=0.004,
            rationale=rationale,
            atr=round(atr, 6),
        )
