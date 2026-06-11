"""
KAVACH-07 — Strategy 5: VP_NODE
LVNs reject price fast; HVNs attract mean reversion.

Setup (LVN rejection):
  • Price at a Low Volume Node (< 20th percentile session volume)
  • Rejection candle (wick + close away from LVN)
  • Trade back toward POC / HVN

Setup (HVN reversion):
  • Price at / beyond Value Area boundary
  • Reverts back toward POC
"""
from __future__ import annotations

from typing import List, Optional

from models import DataSnapshot, Signal
from strategies.base import BaseStrategy
from utils import get_logger

logger = get_logger(__name__)

_LVN_PROXIMITY  = 0.003   # 0.3% tolerance for price being "at" a node
_HVN_PROXIMITY  = 0.003
_MIN_HIST_1H    = 20      # Minimum 1h candles for reliable VP


class VPNode(BaseStrategy):

    @property
    def name(self) -> str:
        return "VP_NODE"

    def get_required_data(self) -> List[str]:
        return ["candles_1h", "poc", "vah", "val", "lvns", "hvns", "atr_1h"]

    def scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        try:
            return self._scan(symbol, data)
        except Exception as e:
            logger.debug(f"VPNode[{symbol}] error: {e}")
            return None

    def _scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        if len(data.candles_1h) < _MIN_HIST_1H:
            return None

        if data.poc < 1e-10:
            return None

        atr = data.atr_1h if data.atr_1h > 0 else data.atr_5m
        if atr < 1e-10:
            return None

        current = data.mid_price
        if current < 1e-10:
            return None

        poc = data.poc
        vah = data.vah
        val = data.val
        lvns = data.lvns
        hvns = data.hvns

        # ── LVN Rejection ─────────────────────────────────────
        for lvn in lvns:
            if lvn < 1e-10:
                continue
            dist_pct = abs(current - lvn) / lvn

            if dist_pct < _LVN_PROXIMITY:
                # Price is at an LVN → expect rejection
                direction = "SHORT" if current >= lvn else "LONG"
                target = poc  # Revert to POC

                if direction == "SHORT":
                    if target >= current:
                        continue   # POC above current, not a short target
                    entry = current
                    sl = entry + 1.0 * atr
                    tp1 = target if target > entry - 2.0 * atr else entry - 2.0 * atr
                else:
                    if target <= current:
                        continue
                    entry = current
                    sl = entry - 1.0 * atr
                    tp1 = target if target < entry + 2.0 * atr else entry + 2.0 * atr

                r = abs(tp1 - entry) / abs(entry - sl) if abs(entry - sl) > 1e-10 else 0
                if r < 1.5:
                    continue   # Not enough reward

                base = 0.50 + min(0.08, (1 - dist_pct / _LVN_PROXIMITY) * 0.08)
                base += 0.03 if data.delta_direction != 0 else 0.0
                confidence = self._clamp_confidence(base)

                rationale = (
                    f"At LVN: {lvn:.4f} (low volume node)\n"
                    f"Session POC: {poc:.4f}\n"
                    f"R/R: {r:.1f} | Rejection expected"
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
                    risk_pct=0.005,
                    rationale=rationale,
                    atr=round(atr, 6),
                )

        # ── Value Area Extension Reversion ────────────────────
        if vah > 0 and val > 0:
            # Price extended above VAH → revert to POC
            if current > vah * (1 + _HVN_PROXIMITY):
                entry = current
                sl = entry + 1.0 * atr
                tp1 = poc
                if tp1 >= entry or abs(tp1 - entry) < abs(entry - sl):
                    return None
                confidence = self._clamp_confidence(0.52)
                rationale = (
                    f"Above VAH: {vah:.4f} (value area high)\n"
                    f"Session POC: {poc:.4f} (reversion target)\n"
                    f"Extended {(current-vah)/vah*100:.1f}% above value area"
                )
                return Signal(
                    symbol=symbol, strategy=self.name, direction="SHORT",
                    confidence=confidence, entry_type="LIMIT",
                    entry_price=round(entry, 6), sl_price=round(sl, 6),
                    tp1_price=round(tp1, 6), risk_pct=0.005,
                    rationale=rationale, atr=round(atr, 6),
                )

            # Price extended below VAL → revert to POC
            if current < val * (1 - _HVN_PROXIMITY):
                entry = current
                sl = entry - 1.0 * atr
                tp1 = poc
                if tp1 <= entry or abs(tp1 - entry) < abs(entry - sl):
                    return None
                confidence = self._clamp_confidence(0.52)
                rationale = (
                    f"Below VAL: {val:.4f} (value area low)\n"
                    f"Session POC: {poc:.4f} (reversion target)\n"
                    f"Extended {(val-current)/val*100:.1f}% below value area"
                )
                return Signal(
                    symbol=symbol, strategy=self.name, direction="LONG",
                    confidence=confidence, entry_type="LIMIT",
                    entry_price=round(entry, 6), sl_price=round(sl, 6),
                    tp1_price=round(tp1, 6), risk_pct=0.005,
                    rationale=rationale, atr=round(atr, 6),
                )

        return None
