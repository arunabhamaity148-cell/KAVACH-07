"""
KAVACH-07 Strategy: Liquidation Fade
Fade the immediate price reaction after a large liquidation event.
Large LONG liq → price drops → fade → LONG (anticipate bounce).
Large SHORT liq → price rises → fade → SHORT (anticipate pullback).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from .base import Signal, StrategyBase
from ..core.indicators import sl_long, sl_short, tp_long, tp_short

logger = logging.getLogger(__name__)


class LiquidationFade(StrategyBase):
    def __init__(self, config: Dict[str, Any], symbol: str) -> None:
        super().__init__(config, symbol)
        self._last_liq_ts: float = 0.0
        self._last_liq_side: str = ""

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        min_liq_usd = float(self._cfg.get("min_liquidation_usd", 500_000))
        sl_pct      = float(self._cfg.get("sl_percent", 0.6))
        tp_pct      = float(self._cfg.get("tp_percent", 1.2))

        # Recent liquidation event from data_context
        liq_event = data_context.get(f"{self.symbol}_liq_event")
        price = md.price

        if liq_event is None:
            return self._neutral("No liquidation event data")

        try:
            liq_size = float(liq_event.get("size_usd", 0.0))
            liq_side = str(liq_event.get("side", ""))   # "LONG" or "SHORT"
            liq_ts   = float(liq_event.get("timestamp", 0.0))

            if liq_size < min_liq_usd:
                return self._neutral(
                    f"Liq size ${liq_size/1e3:.0f}K < ${min_liq_usd/1e3:.0f}K threshold"
                )

            # Only act on fresh events (within 2 minutes)
            age_seconds = time.time() - liq_ts
            if age_seconds > 120:
                return self._neutral(f"Liq event too old ({age_seconds:.0f}s ago)")

            # Avoid double-signalling the same event
            if liq_ts == self._last_liq_ts and liq_side == self._last_liq_side:
                return self._neutral("Already processed this liquidation event")

            self._last_liq_ts   = liq_ts
            self._last_liq_side = liq_side

            size_mult  = liq_size / min_liq_usd
            freshness  = max(0.0, 1.0 - age_seconds / 120.0)
            confidence = min(88.0, 50.0 + size_mult * 10.0 + freshness * 15.0)

            if liq_side == "LONG":
                # Large LONG liquidation → price spike down → fade with LONG
                rationale = (
                    f"LONG LIQ FADE: ${liq_size/1e3:.0f}K long liq {age_seconds:.0f}s ago "
                    f"→ Oversell likely → Bounce LONG"
                )
                return self._create_signal(
                    self.symbol, "LONG", confidence, price,
                    sl_long(price, sl_pct), tp_long(price, tp_pct), rationale
                )

            elif liq_side == "SHORT":
                # Large SHORT liquidation → price spike up → fade with SHORT
                rationale = (
                    f"SHORT LIQ FADE: ${liq_size/1e3:.0f}K short liq {age_seconds:.0f}s ago "
                    f"→ Overbuy likely → Pullback SHORT"
                )
                return self._create_signal(
                    self.symbol, "SHORT", confidence, price,
                    sl_short(price, sl_pct), tp_short(price, tp_pct), rationale
                )

            return self._neutral(f"Unknown liq side: {liq_side}")

        except Exception as exc:
            logger.error("LiquidationFade[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
