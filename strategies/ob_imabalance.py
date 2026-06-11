"""
KAVACH-07 — Strategy 3: OB_IMBALANCE
Depth skew + aligned aggressive flow = short-term continuation.

Setup:
  • Bid/ask depth ratio > 2.0  (long) or < 0.5  (short)
  • Trade delta aligned with imbalance
  • Spread < 0.05% (liquid)
  • Volume above average
"""
from __future__ import annotations

from typing import List, Optional

from models import DataSnapshot, Signal
from strategies.base import BaseStrategy
from utils import get_logger

logger = get_logger(__name__)

_IMBALANCE_LONG  = 2.0    # bid_vol / ask_vol > 2 → buy pressure
_IMBALANCE_SHORT = 0.5    # bid_vol / ask_vol < 0.5 → sell pressure
_MAX_SPREAD_PCT  = 0.0005 # 0.05%
_MIN_VOLUME_RATIO = 1.2


class OBImbalance(BaseStrategy):

    @property
    def name(self) -> str:
        return "OB_IMBALANCE"

    @property
    def min_risk_pct(self) -> float:
        return 0.0025   # Scalp: smaller risk

    @property
    def max_risk_pct(self) -> float:
        return 0.003

    def get_required_data(self) -> List[str]:
        return ["ob_imbalance", "spread_pct", "delta_direction", "volume_ratio", "atr_1m"]

    def scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        try:
            return self._scan(symbol, data)
        except Exception as e:
            logger.debug(f"OBImbalance[{symbol}] error: {e}")
            return None

    def _scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        if not data.bids or not data.asks:
            return None

        imb = data.ob_imbalance
        spread = data.spread_pct
        atr = data.atr_1m
        current = data.mid_price

        if current < 1e-10 or atr < 1e-10:
            return None

        # ── Spread filter ─────────────────────────────────────
        if spread > _MAX_SPREAD_PCT:
            return None

        # ── Volume filter ─────────────────────────────────────
        if data.volume_ratio < _MIN_VOLUME_RATIO:
            return None

        # ── Determine direction ───────────────────────────────
        if imb >= _IMBALANCE_LONG and data.delta_direction >= 0:
            direction = "LONG"
        elif imb <= _IMBALANCE_SHORT and data.delta_direction <= 0:
            direction = "SHORT"
        else:
            return None

        # Scalp: tight levels (0.5× ATR SL, 1.2R TP)
        if direction == "LONG":
            entry = data.best_ask   # Enter at ask for immediacy
            sl    = entry - 0.5 * atr
            tp1   = entry + 0.6 * atr    # ~1.2R
        else:
            entry = data.best_bid
            sl    = entry + 0.5 * atr
            tp1   = entry - 0.6 * atr

        # ── Confidence ────────────────────────────────────────
        base = 0.48
        if direction == "LONG":
            base += min(0.08, (imb - 2.0) * 0.02)
        else:
            base += min(0.08, (0.5 - imb) * 0.04)
        base += 0.03 if data.delta_direction != 0 else 0.0
        base += min(0.04, (data.volume_ratio - 1.2) * 0.04)
        confidence = self._clamp_confidence(base, lo=0.48)

        # ── Compute approx dollar delta ───────────────────────
        bid_vol = sum(b[1] for b in data.bids[:10])
        ask_vol = sum(a[1] for a in data.asks[:10])
        dollar_delta = abs(bid_vol - ask_vol) * current

        rationale = (
            f"Bid/Ask ratio: {imb:.2f}x\n"
            f"Delta: ${dollar_delta:,.0f} ({'aggressive buying' if direction=='LONG' else 'aggressive selling'})\n"
            f"Spread: {spread*100:.3f}%"
        )

        return Signal(
            symbol=symbol,
            strategy=self.name,
            direction=direction,
            confidence=confidence,
            entry_type="MARKET",
            entry_price=round(entry, 6),
            sl_price=round(sl, 6),
            tp1_price=round(tp1, 6),
            risk_pct=self.max_risk_pct,
            rationale=rationale,
            atr=round(atr, 6),
        )
