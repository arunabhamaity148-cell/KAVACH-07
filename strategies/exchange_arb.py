"""
KAVACH-07 — Strategy 10: Cross-Exchange Arbitrage
"""
from __future__ import annotations

from typing import Optional

from models import DataSnapshot, Signal
from strategies.base import BaseStrategy
from utils import get_logger

logger = get_logger(__name__)

class ExchangeArb(BaseStrategy):
    name = "EXCHANGE_ARB"

    def get_required_data(self) -> list:
        return ["bybit_price", "mark_price"]

    def scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        try:
            return self._scan(symbol, data)
        except Exception as e:
            logger.debug(f"ExchangeArb[{symbol}] error: {e}")
            return None

    def _scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        if data.bybit_price <= 0 or data.mark_price <= 0:
            return None

        diff_pct = abs(data.mark_price - data.bybit_price) / data.mark_price
        if diff_pct < 0.0015:
            return None

        direction = "LONG" if data.mark_price < data.bybit_price else "SHORT"
        current = data.mark_price
        atr = data.atr_5m if data.atr_5m > 0 else current * 0.002

        sl_dist = 0.5 * atr
        # FIX: TP1 = 0.7 * atr for R:R = 1.4 (passes 1.2 gate)
        tp1_dist = 0.7 * atr

        if direction == "LONG":
            entry = current
            sl = entry - sl_dist
            tp1 = entry + tp1_dist
        else:
            entry = current
            sl = entry + sl_dist
            tp1 = entry - tp1_dist

        return Signal(
            symbol=symbol,
            strategy=self.name,
            direction=direction,
            confidence=min(diff_pct * 100, 0.75),
            entry_type="MARKET",
            entry_price=entry,
            sl_price=sl,
            tp1_price=tp1,
            risk_pct=0.003,
            rationale=f"Cross-exchange dislocation: {diff_pct*100:.2f}%",
            atr=atr,
        )
