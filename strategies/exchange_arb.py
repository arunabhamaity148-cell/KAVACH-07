"""
KAVACH-07 — Strategy 10: EXCHANGE_ARB
Cross-exchange lead/lag: Bybit moves first, Binance follows.

Setup:
  • Bybit perp price moves > threshold vs Binance
  • Binance has not yet caught up (lag < 50% of Bybit move)
  • Spread and depth acceptable
  • Direction: trade Binance in direction of Bybit's move

Data: Bybit prices fetched by DataEngine._bybit_price_loop().
This is NOT true arbitrage (no simultaneous legs) — it's
a lead/lag signal on the faster exchange signalling direction.
"""
from __future__ import annotations

from typing import List, Optional

from models import DataSnapshot, Signal
from strategies.base import BaseStrategy
from utils import get_logger

logger = get_logger(__name__)

_LEAD_THRESHOLD  = 0.001    # 0.1% move on Bybit to signal
_LAG_THRESHOLD   = 0.5      # Binance must be less than 50% caught up
_MAX_SPREAD      = 0.0008   # 0.08% max spread (requires tight market)
_MIN_BYBIT_PRICE = 1.0      # Bybit price must be valid


class ExchangeArb(BaseStrategy):

    def __init__(self):
        self._bybit_prev: dict = {}   # symbol → last Bybit price snapshot

    @property
    def name(self) -> str:
        return "EXCHANGE_ARB"

    @property
    def min_risk_pct(self) -> float:
        return 0.002

    @property
    def max_risk_pct(self) -> float:
        return 0.0025

    def get_required_data(self) -> List[str]:
        return ["bybit_price", "mark_price", "spread_pct", "atr_5m"]

    def scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        try:
            return self._scan(symbol, data)
        except Exception as e:
            logger.debug(f"ExchangeArb[{symbol}] error: {e}")
            return None

    def _scan(self, symbol: str, data: DataSnapshot) -> Optional[Signal]:
        bybit_now = data.bybit_price
        binance_now = data.mark_price or data.mid_price

        if bybit_now < _MIN_BYBIT_PRICE or binance_now < 1e-10:
            return None

        # ── Spread filter (only liquid pairs) ─────────────────
        if data.spread_pct > _MAX_SPREAD:
            return None

        # ── Need a previous Bybit reading for change calc ─────
        bybit_prev = self._bybit_prev.get(symbol, bybit_now)
        self._bybit_prev[symbol] = bybit_now

        if abs(bybit_prev) < 1e-10:
            return None

        # ── Bybit move ────────────────────────────────────────
        bybit_move = (bybit_now - bybit_prev) / bybit_prev
        if abs(bybit_move) < _LEAD_THRESHOLD:
            return None   # Bybit hasn't moved enough

        # ── Binance lag ───────────────────────────────────────
        # How much has Binance caught up relative to Bybit's move?
        # If Bybit +0.15%, Binance should be lagging
        binance_diff = (binance_now - bybit_prev) / bybit_prev
        catch_up_ratio = binance_diff / bybit_move if abs(bybit_move) > 1e-10 else 1.0

        if catch_up_ratio > _LAG_THRESHOLD:
            return None   # Binance already caught up too much

        atr = data.atr_5m
        if atr < 1e-10:
            return None

        current = binance_now

        # ── Direction = same as Bybit's move ──────────────────
        direction = "LONG" if bybit_move > 0 else "SHORT"

        # ── Tight levels (fast reversal expected) ─────────────
        if direction == "LONG":
            entry = current
            sl    = entry - 0.5 * atr
            tp1   = entry + 0.5 * atr   # ~1R (quick target)
        else:
            entry = current
            sl    = entry + 0.5 * atr
            tp1   = entry - 0.5 * atr

        # ── Confidence ────────────────────────────────────────
        base = 0.50
        base += min(0.08, abs(bybit_move) * 30)
        base += min(0.05, (1 - catch_up_ratio) * 0.08)
        confidence = self._clamp_confidence(base, lo=0.50)

        lag_pct = (1 - catch_up_ratio) * abs(bybit_move)

        rationale = (
            f"Bybit lead: {bybit_move*100:+.3f}%\n"
            f"Binance lag: {lag_pct*100:.3f}% (catching up)\n"
            f"Spread: {data.spread_pct*100:.3f}%\n"
            f"Depth: sufficient"
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
