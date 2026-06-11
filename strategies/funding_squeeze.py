"""
KAVACH-07 — Strategy 2: FUNDING_SQUEEZE
Extreme funding rate → crowded side → forced unwind → reversal.

Setup:
  • Funding rate at ≥90th or ≤10th percentile (30-day window)
  • OI is contracting (negative delta over 1h)
  • Trade delta flipping direction (crowded side starting to close)

Fade the overcrowded side.
"""
from __future__ import annotations

from typing import List, Optional

from models import DataSnapshot, Signal
from strategies.base import BaseStrategy
from utils import get_logger

logger = get_logger(__name__)

# Funding rate thresholds
_EXTREME_HIGH_PCT = 90.0   # Longs paying — fade longs (SHORT)
_EXTREME_LOW_PCT  = 10.0   # Shorts paying — fade shorts (LONG)
_OI_CONTRACT_THRESHOLD = -0.03   # OI dropping 3%+ in 1h


class FundingSqueeze(BaseStrategy):

    @property
    def name(self) -> str:
        return "FUNDING_SQUEEZE"

    def get_required_data(self) -> List[str]:
        return ["funding_rate", "funding_percentile", "oi_change_1h", "delta_direction", "atr_1h"]

    def scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        try:
            return self._scan(symbol, data)
        except Exception as e:
            logger.debug(f"FundingSqueeze[{symbol}] error: {e}")
            return None

    def _scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        if len(data.funding_history) < 30:
            return None   # Need history for percentile

        pct = data.funding_percentile
        rate = data.funding_rate
        atr = data.atr_1h if data.atr_1h > 0 else data.atr_5m
        if atr < 1e-10:
            return None

        current = data.mid_price
        if current < 1e-10:
            return None

        # ── Determine direction ───────────────────────────────
        if pct >= _EXTREME_HIGH_PCT:
            # Extremely positive funding → longs crowded → SHORT
            direction = "SHORT"
        elif pct <= _EXTREME_LOW_PCT:
            # Extremely negative funding → shorts crowded → LONG
            direction = "LONG"
        else:
            return None   # Funding not extreme

        # ── OI must be contracting ────────────────────────────
        if data.oi_change_1h > _OI_CONTRACT_THRESHOLD:
            return None

        # ── Delta must confirm flip ───────────────────────────
        # For SHORT setup: need selling delta (direction == -1 or 0)
        # For LONG setup: need buying delta (direction == 1 or 0)
        if direction == "SHORT" and data.delta_direction == 1:
            return None   # Delta still bullish, not flipping yet
        if direction == "LONG" and data.delta_direction == -1:
            return None

        # ── Price levels ──────────────────────────────────────
        if direction == "SHORT":
            entry = current
            sl = entry + 1.2 * atr
            tp1 = entry - 2.0 * (sl - entry)
            tp2 = entry - 3.0 * (sl - entry)
        else:
            entry = current
            sl = entry - 1.2 * atr
            tp1 = entry + 2.0 * (entry - sl)
            tp2 = entry + 3.0 * (entry - sl)

        # ── Confidence ────────────────────────────────────────
        extreme_dist = abs(pct - 50) - 40   # 0 at 90/10 pct, grows beyond
        base = 0.50
        base += min(0.08, extreme_dist * 0.005)
        base += min(0.05, abs(data.oi_change_1h) * 1.5)
        confidence = self._clamp_confidence(base)

        rationale = (
            f"Funding: {rate*100:.4f}% ({pct:.0f}th percentile)\n"
            f"OI change: {data.oi_change_1h*100:.1f}% in 1h\n"
            f"Delta flip: {'BUY→SELL' if direction=='SHORT' else 'SELL→BUY'}"
        )

        return Signal(
            symbol=symbol,
            strategy=self.name,
            direction=direction,
            confidence=confidence,
            entry_type="MARKET",
            entry_price=round(current, 6),
            sl_price=round(sl, 6),
            tp1_price=round(tp1, 6),
            tp2_price=round(tp2, 6),
            risk_pct=0.005,
            rationale=rationale,
            atr=round(atr, 6),
        )
