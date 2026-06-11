"""
KAVACH-07 — Strategy 8: REGIME_FILTER
Not a standalone trading signal — sets the global market regime.

Regime is derived from:
  • Average funding rate across all tracked pairs
  • Breadth: how many pairs have positive OI trend
  • CVD aggregate direction
  • Fear & Greed index (extreme readings)

Output is written to DataEngine.set_regime() and used by
SignalEngine to apply position multipliers.

As a Signal subtype (for Telegram notification), returns a
regime update alert when bias changes.
"""
from __future__ import annotations

from typing import List, Optional

from models import DataSnapshot, RegimeSignal, Signal
from strategies.base import BaseStrategy
from utils import get_logger

logger = get_logger(__name__)

_BULLISH_THRESHOLD = 0.55    # >55% of indicators bullish
_BEARISH_THRESHOLD = 0.45    # <45%


class RegimeFilter(BaseStrategy):
    """
    Unlike other strategies, this runs on a single aggregated
    DataSnapshot (symbol='AGGREGATE') built by SignalEngine.
    """

    def __init__(self):
        self._last_bias: str = "NEUTRAL"

    @property
    def name(self) -> str:
        return "REGIME_FILTER"

    def get_required_data(self) -> List[str]:
        return ["funding_rate", "oi_change_1h", "fear_greed_index", "cvd_z_score"]

    def scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        """
        For REGIME_FILTER we don't return a trade signal.
        Returns None always — use compute_regime() instead.
        """
        return None

    def compute_regime(self, snapshots: List[DataSnapshot]) -> RegimeSignal:
        """
        Compute global regime from all symbol snapshots.
        Returns a RegimeSignal.
        """
        if not snapshots:
            return RegimeSignal(bias="NEUTRAL", confidence=0.5,
                                position_multiplier=1.0)

        bullish_votes = 0
        total_votes = 0
        avg_funding = 0.0
        oi_trend_sum = 0.0
        cvd_sum = 0.0
        count = 0

        for snap in snapshots:
            count += 1

            # Funding vote: negative funding = bullish, positive = bearish
            avg_funding += snap.funding_rate
            if snap.funding_rate < 0:
                bullish_votes += 1
            elif snap.funding_rate < 0.0001:  # near-zero = neutral = slight bullish
                bullish_votes += 0.5
            total_votes += 1

            # OI vote: expanding = confirms move, but direction?
            # Use OI + funding combo: positive funding + expanding OI = late bull
            oi_trend_sum += snap.oi_change_1h
            if snap.oi_change_1h > 0.02 and snap.funding_rate < 0.0001:
                bullish_votes += 1
                total_votes += 1
            elif snap.oi_change_1h < -0.02:
                total_votes += 1   # Contracting OI — neutral (not directional)

            # CVD vote
            cvd_sum += snap.cvd_z_score
            if snap.cvd_z_score > 0.5:
                bullish_votes += 0.5
                total_votes += 0.5
            elif snap.cvd_z_score < -0.5:
                total_votes += 0.5

        if count == 0 or total_votes < 1:
            return RegimeSignal(bias="NEUTRAL", confidence=0.5,
                                position_multiplier=1.0)

        # Fear & Greed (use first snapshot — it's global)
        fg = snapshots[0].fear_greed_index if snapshots else 50

        # F&G vote
        if fg > 75:
            # Extreme greed → bearish contrarian signal
            total_votes += 2
            # No bullish votes added
        elif fg < 25:
            # Extreme fear → bullish contrarian signal
            bullish_votes += 2
            total_votes += 2
        elif fg > 60:
            total_votes += 1  # slightly bearish bias
        elif fg < 40:
            bullish_votes += 1
            total_votes += 1

        avg_funding /= count
        oi_trend = oi_trend_sum / count
        cvd_avg = cvd_sum / count

        bull_ratio = bullish_votes / total_votes if total_votes > 0 else 0.5

        if bull_ratio >= _BULLISH_THRESHOLD:
            bias = "BULLISH"
            confidence = min(0.80, 0.50 + (bull_ratio - 0.5) * 1.0)
            multiplier = 1.0
        elif bull_ratio <= _BEARISH_THRESHOLD:
            bias = "BEARISH"
            confidence = min(0.80, 0.50 + (0.5 - bull_ratio) * 1.0)
            multiplier = 0.5   # Reduce all position sizes by 50%
        else:
            bias = "NEUTRAL"
            confidence = 0.50
            multiplier = 0.75

        regime = RegimeSignal(
            bias=bias,
            confidence=round(confidence, 3),
            avg_funding=round(avg_funding, 6),
            oi_trend=round(oi_trend, 4),
            position_multiplier=multiplier,
        )

        if bias != self._last_bias:
            self._last_bias = bias
            logger.info(
                f"REGIME CHANGE → {bias} "
                f"(bull_ratio={bull_ratio:.2f}, F&G={fg}, "
                f"avg_funding={avg_funding*100:.4f}%, "
                f"size_mult={multiplier:.1f}x)"
            )

        return regime
