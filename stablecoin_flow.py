"""
KAVACH-07 Strategy: Stablecoin Flow
On-chain stablecoin inflow → buying pressure (LONG).
On-chain stablecoin outflow → selling pressure (SHORT).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import Signal, StrategyBase
from ..core.indicators import sl_long, sl_short, tp_long, tp_short

logger = logging.getLogger(__name__)


class StablecoinFlow(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        min_flow   = float(self._cfg.get("min_flow_value_usd", 30_000_000))
        sl_pct     = float(self._cfg.get("sl_percent", 1.0))
        tp_pct     = float(self._cfg.get("tp_percent", 2.0))

        sc_flow = md.stablecoin_net_flow
        price   = md.price

        if sc_flow == 0.0:
            return self._neutral("Stablecoin flow data unavailable")

        try:
            if sc_flow >= min_flow:
                magnitude = sc_flow / min_flow
                confidence = min(78.0, 42.0 + magnitude * 9.0)
                rationale = (
                    f"STABLECOIN INFLOW: ${sc_flow/1e6:.1f}M "
                    f"(min ${min_flow/1e6:.0f}M) → Fresh capital entering → LONG"
                )
                return self._create_signal(
                    self.symbol, "LONG", confidence, price,
                    sl_long(price, sl_pct), tp_long(price, tp_pct), rationale
                )

            elif sc_flow <= -min_flow:
                magnitude = abs(sc_flow) / min_flow
                confidence = min(78.0, 42.0 + magnitude * 9.0)
                rationale = (
                    f"STABLECOIN OUTFLOW: ${sc_flow/1e6:.1f}M "
                    f"(min ${min_flow/1e6:.0f}M) → Capital leaving exchanges → SHORT"
                )
                return self._create_signal(
                    self.symbol, "SHORT", confidence, price,
                    sl_short(price, sl_pct), tp_short(price, tp_pct), rationale
                )

            return self._neutral(
                f"Stablecoin flow ${sc_flow/1e6:.1f}M below threshold ${min_flow/1e6:.0f}M"
            )

        except Exception as exc:
            logger.error("StablecoinFlow[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
