"""
KAVACH-07 — Strategy 9: SOCIAL_FADE
Extreme crowd attention signals a late move. Fade with price confirmation.

Data sources (all free):
  • Fear & Greed Index via api.alternative.me (fetched by DataEngine)
  • Social proxy: abnormal volume/trade-count spike (Binance @aggTrade)
  • Price exhaustion: bearish/bullish engulfing on 1h, extended from mean

Setup:
  • F&G extreme (> 75 extreme greed or < 25 extreme fear)
  • Volume spike > 2.5x 20-period average (crowd pile-in)
  • Price exhaustion candle (long wick, large body against trend)
"""
from __future__ import annotations

from typing import List, Optional

from models import DataSnapshot, Signal
from strategies.base import BaseStrategy
from utils import get_logger

logger = get_logger(__name__)

_FG_EXTREME_GREED = 75
_FG_EXTREME_FEAR  = 25
_VOLUME_SPIKE     = 2.5    # 2.5x average volume
_PRICE_MOVE_1H    = 0.10   # 10% move in last few 1h candles = extended


class SocialFade(BaseStrategy):

    @property
    def name(self) -> str:
        return "SOCIAL_FADE"

    def get_required_data(self) -> List[str]:
        return ["fear_greed_index", "volume_ratio", "candles_1h", "atr_1h"]

    def scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        try:
            return self._scan(symbol, data)
        except Exception as e:
            logger.debug(f"SocialFade[{symbol}] error: {e}")
            return None

    def _scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        fg = data.fear_greed_index
        vol_ratio = data.volume_ratio
        c1h = data.candles_1h
        atr = data.atr_1h if data.atr_1h > 0 else data.atr_5m

        if len(c1h) < 10 or atr < 1e-10:
            return None

        current = data.mid_price
        if current < 1e-10:
            return None

        # ── F&G must be extreme ───────────────────────────────
        if _FG_EXTREME_FEAR < fg < _FG_EXTREME_GREED:
            return None

        # ── Volume spike (crowd pile-in) ──────────────────────
        if vol_ratio < _VOLUME_SPIKE:
            return None

        # ── Price exhaustion: large recent move ───────────────
        lookback = min(4, len(c1h) - 1)
        ref_close = float(c1h[-(lookback + 1)]["close"])
        if ref_close < 1e-10:
            return None

        move_pct = (current - ref_close) / ref_close

        # ── Determine fade direction ──────────────────────────
        if fg >= _FG_EXTREME_GREED and move_pct > 0.05:
            direction = "SHORT"
            price_move_label = f"+{move_pct*100:.1f}% in {lookback}h"
        elif fg <= _FG_EXTREME_FEAR and move_pct < -0.05:
            direction = "LONG"
            price_move_label = f"{move_pct*100:.1f}% in {lookback}h"
        else:
            return None   # Move not large enough to be exhausted

        # ── Confirm with wick exhaustion (last 1h candle) ─────
        last = c1h[-1]
        body = abs(float(last["close"]) - float(last["open"]))
        total_range = float(last["high"]) - float(last["low"])

        if total_range > 0:
            wick_ratio = 1 - (body / total_range)  # high wick ratio = exhaustion
        else:
            wick_ratio = 0.0

        # For SHORT fade: we want a long upper wick (selling into strength)
        # For LONG fade: we want a long lower wick (buying into weakness)
        if direction == "SHORT":
            upper_wick = float(last["high"]) - max(float(last["close"]), float(last["open"]))
            exhaustion = upper_wick / total_range if total_range > 0 else 0
        else:
            lower_wick = min(float(last["close"]), float(last["open"])) - float(last["low"])
            exhaustion = lower_wick / total_range if total_range > 0 else 0

        # Need at least 30% wick for confirmation, or skip if very extreme F&G
        if exhaustion < 0.30 and abs(fg - 50) < 30:
            return None

        # ── Price levels ──────────────────────────────────────
        if direction == "SHORT":
            entry = current
            sl    = entry + 1.2 * atr
            tp1   = entry - 1.8 * (sl - entry)
        else:
            entry = current
            sl    = entry - 1.2 * atr
            tp1   = entry + 1.8 * (entry - sl)

        # ── Confidence ────────────────────────────────────────
        base = 0.50
        base += min(0.07, (abs(fg - 50) - 25) * 0.005)
        base += min(0.05, (vol_ratio - 2.5) * 0.02)
        base += min(0.04, exhaustion * 0.12)
        confidence = self._clamp_confidence(base)

        rationale = (
            f"F&G: {fg} ({'extreme greed' if fg > 75 else 'extreme fear'})\n"
            f"Volume spike: {vol_ratio:.1f}x 20-period avg\n"
            f"Price: {price_move_label} (exhaustion)\n"
            f"Wick exhaustion: {exhaustion*100:.0f}%"
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
