"""
KAVACH-07 Strategy: Post-Settlement Reversion
Trade mean reversion after quarterly/monthly futures contract settlement.
Settlement causes price distortion; reversion to pre-settlement level is expected.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .base import Signal, StrategyBase
from ..core.indicators import klines_to_ohlcv, sl_long, sl_short, tp_long, tp_short

logger = logging.getLogger(__name__)

# Binance USDT-M quarterly settlement dates (approximate last Fridays of Mar/Jun/Sep/Dec)
# Updated for 2025-2026; add more as needed
_SETTLEMENT_DATES_UTC = [
    (2025,  3, 28, 8, 0),
    (2025,  6, 27, 8, 0),
    (2025,  9, 26, 8, 0),
    (2025, 12, 26, 8, 0),
    (2026,  3, 27, 8, 0),
    (2026,  6, 26, 8, 0),
]


def _nearest_settlement_age_seconds() -> Optional[float]:
    """Return seconds since the most recent settlement, or None if too far."""
    now = datetime.now(tz=timezone.utc)
    min_age = None
    for y, mo, d, hh, mm in _SETTLEMENT_DATES_UTC:
        try:
            dt = datetime(y, mo, d, hh, mm, tzinfo=timezone.utc)
            age = (now - dt).total_seconds()
            if 0 <= age <= 24 * 3600:   # within 24h after settlement
                if min_age is None or age < min_age:
                    min_age = age
        except ValueError:
            continue
    return min_age


class PostSettlement(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        lookback_h   = int(self._cfg.get("settlement_lookback_hours", 4))
        revert_tgt   = float(self._cfg.get("reversion_target_percent", 1.5))
        sl_pct       = float(self._cfg.get("sl_percent", 1.0))
        tp_pct       = float(self._cfg.get("tp_percent", 2.0))

        try:
            settlement_age = _nearest_settlement_age_seconds()
            if settlement_age is None:
                return self._neutral("No recent settlement (>24h ago)")

            # Get price N hours before settlement from kline history
            klines = md.kline_data
            if len(klines) < 30:
                return self._neutral("Insufficient kline history for settlement analysis")

            o, h, l, c, v = klines_to_ohlcv(klines)

            # Approximate bars for lookback (assuming 5m klines → 12 bars/hour)
            bars_lookback = min(len(c) - 1, lookback_h * 12)
            pre_settlement_price = float(c[-bars_lookback - 1]) if bars_lookback > 0 else float(c[0])
            current_price        = md.price

            distortion = (current_price - pre_settlement_price) / (pre_settlement_price + 1e-10)

            # Freshness score: more confident closer to settlement
            freshness = max(0.0, 1.0 - settlement_age / (24 * 3600))
            age_h = settlement_age / 3600.0

            if abs(distortion) < 0.005:
                return self._neutral(
                    f"Post-settlement ({age_h:.1f}h ago) but minimal distortion "
                    f"({distortion*100:.2f}%)"
                )

            distortion_magnitude = abs(distortion) / 0.005
            confidence = min(72.0, 38.0 + distortion_magnitude * 8.0 + freshness * 15.0)

            if distortion > 0:
                # Price rose above pre-settlement → revert down → SHORT
                rationale = (
                    f"POST-SETTLEMENT SHORT: {age_h:.1f}h since settlement | "
                    f"Price +{distortion*100:.2f}% above pre-settlement {pre_settlement_price:.4f} "
                    f"→ Reversion expected"
                )
                return self._create_signal(
                    self.symbol, "SHORT", confidence, current_price,
                    sl_short(current_price, sl_pct), tp_short(current_price, tp_pct), rationale
                )
            else:
                # Price fell below pre-settlement → revert up → LONG
                rationale = (
                    f"POST-SETTLEMENT LONG: {age_h:.1f}h since settlement | "
                    f"Price {distortion*100:.2f}% below pre-settlement {pre_settlement_price:.4f} "
                    f"→ Reversion expected"
                )
                return self._create_signal(
                    self.symbol, "LONG", confidence, current_price,
                    sl_long(current_price, sl_pct), tp_long(current_price, tp_pct), rationale
                )

        except Exception as exc:
            logger.error("PostSettlement[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
