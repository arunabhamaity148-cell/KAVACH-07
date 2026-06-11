"""
KAVACH-07 — Strategy 4: LIQUIDITY_SWEEP
Stop hunts above/below swing highs/lows, then price reclaims.

Setup:
  • Price sweeps beyond swing high/low by at least 0.1% (stop hunt)
  • Then closes back inside the swing range
  • Delta confirms opposite flow (smart money absorbed the liquidity)
"""
from __future__ import annotations

from typing import List, Optional

from models import DataSnapshot, Signal
from strategies.base import BaseStrategy
from utils import get_logger

logger = get_logger(__name__)

_MIN_SWEEP_PCT   = 0.001   # 0.1% beyond swing
_DELTA_CONFIRM   = 0       # delta_direction: must be >= 0 for LONG (non-negative)


class LiquiditySweep(BaseStrategy):

    @property
    def name(self) -> str:
        return "LIQUIDITY_SWEEP"

    def get_required_data(self) -> List[str]:
        return ["candles_5m", "swing_high_5m", "swing_low_5m", "delta_direction", "atr_5m"]

    def scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        try:
            return self._scan(symbol, data)
        except Exception as e:
            logger.debug(f"LiquiditySweep[{symbol}] error: {e}")
            return None

    def _scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        c5m = data.candles_5m
        if len(c5m) < 25:
            return None

        atr = data.atr_5m
        if atr < 1e-10:
            return None

        current = data.mid_price
        if current < 1e-10:
            return None

        swing_high = data.swing_high_5m
        swing_low  = data.swing_low_5m

        if swing_high < 1e-10 or swing_low < 1e-10:
            return None

        # Look at the last 3 candles (recent price action)
        recent = c5m[-3:]
        if not recent:
            return None

        candle_high = max(float(c["high"]) for c in recent)
        candle_low  = min(float(c["low"])  for c in recent)
        last_close  = float(c5m[-1]["close"])

        sweep_direction = None

        # ── High sweep: wick above swing high, close back below ──
        if candle_high > swing_high * (1 + _MIN_SWEEP_PCT):
            if last_close < swing_high:  # reclaimed back inside
                sweep_direction = "LONG"  # Sweep of highs → trapped longs → fade SHORT? 
                # Actually: stops above swing high get taken → sell-off absorbed → LONG
                # This is Smart Money concept: high sweep triggers short stops, then MM buys
                sweep_direction = "LONG"

        # ── Low sweep: wick below swing low, close back above ──
        if candle_low < swing_low * (1 - _MIN_SWEEP_PCT):
            if last_close > swing_low:  # reclaimed back above
                sweep_direction = "SHORT"
                # Low sweep triggers long stops, then MM sells → SHORT
                # Wait — spec says LONG for low sweep (price bounces after stops taken)
                # Standard ICT: sweep low → liquidity grabbed → bullish reversal
                sweep_direction = "LONG"

        # Re-evaluate properly per standard sweep logic:
        # HIGH sweep = wick above key high → SHORT (exhaustion after stops hit)
        # LOW sweep = wick below key low → LONG (bullish reversal after stops hit)
        sweep_direction = None

        if candle_high > swing_high * (1 + _MIN_SWEEP_PCT) and last_close < swing_high:
            sweep_direction = "SHORT"   # Swept highs, rejected back below
            sweep_price = candle_high
            if data.delta_direction != -1 and data.delta_direction != 0:
                return None  # Delta must show selling

        if candle_low < swing_low * (1 - _MIN_SWEEP_PCT) and last_close > swing_low:
            sweep_direction = "LONG"    # Swept lows, recovered above
            sweep_price = candle_low
            if data.delta_direction != 1 and data.delta_direction != 0:
                return None  # Delta must show buying

        if sweep_direction is None:
            return None

        # ── CVD delta must confirm ────────────────────────────
        if sweep_direction == "LONG" and data.delta_direction == -1:
            return None
        if sweep_direction == "SHORT" and data.delta_direction == 1:
            return None

        # ── Price levels ──────────────────────────────────────
        if sweep_direction == "LONG":
            entry  = current
            sl     = swing_low - 0.2 * atr   # Below the swept low
            sl     = min(sl, entry - 1.0 * atr)
            tp1    = entry + 2.0 * (entry - sl)
            tp2    = entry + 2.8 * (entry - sl)
        else:
            entry  = current
            sl     = swing_high + 0.2 * atr  # Above the swept high
            sl     = max(sl, entry + 1.0 * atr)
            tp1    = entry - 2.0 * (sl - entry)
            tp2    = entry - 2.8 * (sl - entry)

        sweep_pct = abs(candle_high - swing_high) / swing_high \
            if sweep_direction == "SHORT" else \
            abs(swing_low - candle_low) / swing_low

        # ── Confidence ────────────────────────────────────────
        base = 0.52
        base += min(0.08, sweep_pct * 20)
        base += 0.04 if data.delta_direction != 0 else 0.0
        base += min(0.03, data.volume_ratio * 0.02)
        confidence = self._clamp_confidence(base)

        direction_label = "above swing high" if sweep_direction == "SHORT" else "below swing low"
        level = swing_high if sweep_direction == "SHORT" else swing_low
        delta_desc = "+$" + f"{abs(data.delta_1m):,.0f}" if data.delta_1m else "confirmed"

        rationale = (
            f"Sweep: {current:.4f} ({direction_label} @ {level:.4f})\n"
            f"Reclaim: {current:.4f} (back inside range)\n"
            f"Delta: {delta_desc} ({'buying' if sweep_direction=='LONG' else 'selling'} pressure)"
        )

        return Signal(
            symbol=symbol,
            strategy=self.name,
            direction=sweep_direction,
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
