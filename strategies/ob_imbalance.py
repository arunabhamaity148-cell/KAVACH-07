"""
KAVACH-07 — Strategy 3: Orderbook Imbalance Scalp
"""
from __future__ import annotations

from typing import Optional

from models import DataSnapshot, Signal
from strategies.base import BaseStrategy
from utils import get_logger

logger = get_logger(__name__)

class OBImbalance(BaseStrategy):
    name = "OB_IMBALANCE"

    def get_required_data(self) -> list:
        return ["orderbook"]

    def scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        try:
            return self._scan(symbol, data)
        except Exception as e:
            logger.debug(f"OBImbalance[{symbol}] error: {e}")
            return None

    def _scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        imbalance = data.ob_imbalance
        if imbalance < 2.0 and imbalance > 0.5:
            return None

        direction = "LONG" if imbalance >= 2.0 else "SHORT"
        current = data.mid_price
        atr = data.atr_1m if data.atr_1m > 0 else current * 0.002

        sl_dist = 0.4 * atr
        tp1_dist = 0.8 * atr

        if direction == "LONG":
            # FIX: data.best_ask now works via DataSnapshot property
            entry = data.best_ask if data.best_ask > 0 else current
            sl = entry - sl_dist
            tp1 = entry + tp1_dist
        else:
            entry = data.best_bid if data.best_bid > 0 else current
            sl = entry + sl_dist
            tp1 = entry - tp1_dist

        return Signal(
            symbol=symbol,
            strategy=self.name,
            direction=direction,
            confidence=min(abs(imbalance - 1) / 3, 0.7),
            entry_type="LIMIT",
            entry_price=entry,
            sl_price=sl,
            tp1_price=tp1,
            risk_pct=0.003,
            rationale=f"OB imbalance: {imbalance:.2f}",
            atr=atr,
        )
