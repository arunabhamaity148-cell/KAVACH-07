"""
KAVACH-07 — Strategy 1: LIQUIDATION_FADE
Forced liquidations create a price vacuum that tends to revert.

Setup:
  • 5-min price impulse  > 2%
  • OI drop > 5% in last hour (forced exits)
  • CVD z-score extreme (|z| > 2.5 std-dev)
  • Price has reclaimed > 50% of the impulse

Direction is opposite to the impulse (fade the move).
"""
from __future__ import annotations

from typing import List, Optional

from models import DataSnapshot, Signal
from strategies.base import BaseStrategy
from utils import get_logger

logger = get_logger(__name__)


class LiquidationFade(BaseStrategy):

    @property
    def name(self) -> str:
        return "LIQUIDATION_FADE"

    def get_required_data(self) -> List[str]:
        return ["candles_5m", "cvd_z_score", "oi_change_1h", "atr_5m"]

    def scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        try:
            return self._scan(symbol, data)
        except Exception as e:
            logger.debug(f"LiquidationFade[{symbol}] error: {e}")
            return None

    def _scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        c5m = data.candles_5m
        if len(c5m) < 10:
            return None

        atr = data.atr_5m
        if atr < 1e-10:
            return None

        # ── Detect impulse: compare last closed candle to 5 candles back ──
        lookback = 5
        if len(c5m) < lookback + 2:
            return None

        ref_close = float(c5m[-(lookback + 1)]["close"])
        prev_close = float(c5m[-2]["close"])
        current = data.mid_price or float(c5m[-1]["close"])

        if ref_close < 1e-10:
            return None

        impulse_pct = (prev_close - ref_close) / ref_close

        # Need at least 2% impulse
        if abs(impulse_pct) < 0.02:
            return None

        # ── OI must have dropped (liquidation-driven) ──
        if data.oi_change_1h > -0.05:   # less than 5% OI drop
            return None

        # ── CVD must be extreme ──
        if abs(data.cvd_z_score) < 2.5:
            return None

        # ── Check reclaim ──
        if impulse_pct < 0:
            # Downward impulse → expect LONG bounce
            # Impulse: from ref_close down to trough (approx prev_close)
            trough = min(prev_close, float(c5m[-1]["low"]))
            impulse_range = ref_close - trough
            if impulse_range < 1e-10:
                return None
            reclaim = (current - trough) / impulse_range

            if reclaim < 0.50:
                return None
            if data.cvd_z_score > 0:   # CVD should be negative (selling)
                return None

            direction = "LONG"
            entry = current
            sl = entry - 1.5 * atr
            tp1 = entry + 2.5 * (entry - sl)
            tp2 = entry + 3.5 * (entry - sl)

        else:
            # Upward impulse → expect SHORT fade
            peak = max(prev_close, float(c5m[-1]["high"]))
            impulse_range = peak - ref_close
            if impulse_range < 1e-10:
                return None
            reclaim = (peak - current) / impulse_range

            if reclaim < 0.50:
                return None
            if data.cvd_z_score < 0:   # CVD should be positive (buying)
                return None

            direction = "SHORT"
            entry = current
            sl = entry + 1.5 * atr
            tp1 = entry - 2.5 * (sl - entry)
            tp2 = entry - 3.5 * (sl - entry)

        # ── Confidence ────────────────────────────────────────
        base = 0.50
        base += min(0.08, abs(impulse_pct) * 2.0)
        base += min(0.06, abs(data.oi_change_1h))
        base += min(0.05, (abs(data.cvd_z_score) - 2.5) * 0.02)
        base += min(0.04, reclaim * 0.04)
        confidence = self._clamp_confidence(base)

        rationale = (
            f"5min impulse: {impulse_pct*100:.1f}% (liquidation spike)\n"
            f"OI drop: {data.oi_change_1h*100:.1f}% in 1h\n"
            f"CVD z-score: {data.cvd_z_score:.2f}σ (extreme flow)\n"
            f"Reclaim: {reclaim*100:.0f}% of impulse recovered"
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
            tp2_price=round(tp2, 6),
            risk_pct=0.005,
            rationale=rationale,
            atr=round(atr, 6),
        )
