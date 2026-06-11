"""
KAVACH-07 — Strategy 6: OI_BREAKOUT
Real breakouts require OI expansion + participation.
Fake breakouts have static or declining OI.

Setup:
  • Price breaks out of recent range by > 2%
  • OI expanding > 5% in 1h (new money entering)
  • Spread stable (not a thin-market spike)
  • Orderbook imbalance supporting direction
"""
from __future__ import annotations

from typing import List, Optional

from models import DataSnapshot, Signal
from strategies.base import BaseStrategy
from utils import price_change_pct, get_logger

logger = get_logger(__name__)

_BREAKOUT_PCT    = 0.02    # 2% price move from range
_OI_EXPAND_MIN   = 0.05    # 5% OI increase confirms new money
_MAX_SPREAD      = 0.001   # 0.1% spread max (liquid market)
_MIN_VOLUME      = 1.5     # Volume must be 1.5x avg


class OIBreakout(BaseStrategy):

    @property
    def name(self) -> str:
        return "OI_BREAKOUT"

    def get_required_data(self) -> List[str]:
        return ["candles_1h", "oi_change_1h", "spread_pct", "ob_imbalance",
                "volume_ratio", "atr_1h"]

    def scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        try:
            return self._scan(symbol, data)
        except Exception as e:
            logger.debug(f"OIBreakout[{symbol}] error: {e}")
            return None

    def _scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        c1h = data.candles_1h
        if len(c1h) < 25:
            return None

        atr = data.atr_1h if data.atr_1h > 0 else data.atr_5m
        if atr < 1e-10:
            return None

        # ── OI must be expanding ──────────────────────────────
        if data.oi_change_1h < _OI_EXPAND_MIN:
            return None

        # ── Spread must be acceptable ─────────────────────────
        if data.spread_pct > _MAX_SPREAD:
            return None

        # ── Volume must be elevated ───────────────────────────
        if data.volume_ratio < _MIN_VOLUME:
            return None

        current = data.mid_price
        if current < 1e-10:
            return None

        # ── Find range over last 20 1h candles (excluding last 2) ──
        range_candles = c1h[-22:-2]
        if len(range_candles) < 10:
            return None

        range_high = max(float(c["high"]) for c in range_candles)
        range_low  = min(float(c["low"])  for c in range_candles)

        if range_high < 1e-10 or range_low < 1e-10:
            return None

        # ── Check breakout ────────────────────────────────────
        up_break   = (current - range_high) / range_high
        down_break = (range_low - current) / range_low

        if up_break >= _BREAKOUT_PCT and data.ob_imbalance >= 1.2:
            direction = "LONG"
            breakout_pct = up_break
        elif down_break >= _BREAKOUT_PCT and data.ob_imbalance <= 0.85:
            direction = "SHORT"
            breakout_pct = down_break
        else:
            return None

        # ── Price levels ──────────────────────────────────────
        if direction == "LONG":
            entry  = current
            sl     = entry - 1.5 * atr
            tp1    = entry + 2.5 * (entry - sl)
            tp2    = entry + 3.5 * (entry - sl)
        else:
            entry  = current
            sl     = entry + 1.5 * atr
            tp1    = entry - 2.5 * (sl - entry)
            tp2    = entry - 3.5 * (sl - entry)

        # ── Confidence ────────────────────────────────────────
        base = 0.50
        base += min(0.07, breakout_pct * 2)
        base += min(0.06, (data.oi_change_1h - 0.05) * 0.5)
        base += min(0.04, (data.volume_ratio - 1.5) * 0.02)
        confidence = self._clamp_confidence(base)

        rationale = (
            f"Breakout: +{breakout_pct*100:.1f}% {'above resistance' if direction=='LONG' else 'below support'}\n"
            f"OI change: +{data.oi_change_1h*100:.1f}% in 1h (new money)\n"
            f"Spread: {data.spread_pct*100:.3f}% (stable)\n"
            f"Volume: {data.volume_ratio:.1f}x avg"
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
            tp2_price=round(tp2, 6),
            risk_pct=0.005,
            rationale=rationale,
            atr=round(atr, 6),
        )
