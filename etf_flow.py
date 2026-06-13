"""
KAVACH-07 Strategy: ETF Flow Proxy
Gauge institutional sentiment via Bitcoin ETF net flow proxy.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import Signal, StrategyBase
from ..core.indicators import sl_long, sl_short, tp_long, tp_short

logger = logging.getLogger(__name__)


class EtfFlow(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        pos_thresh = float(self._cfg.get("positive_flow_threshold_usd", 50_000_000))
        neg_thresh = float(self._cfg.get("negative_flow_threshold_usd", -50_000_000))
        sl_pct     = float(self._cfg.get("sl_percent", 1.0))
        tp_pct     = float(self._cfg.get("tp_percent", 2.0))

        etf_flow = md.etf_net_flow
        price    = md.price

        if etf_flow == 0.0:
            return self._neutral("ETF flow data unavailable")

        try:
            if etf_flow >= pos_thresh:
                magnitude = etf_flow / pos_thresh
                confidence = min(80.0, 45.0 + magnitude * 10.0)
                rationale = (
                    f"POSITIVE ETF net flow: ${etf_flow/1e6:.1f}M "
                    f"(threshold ${pos_thresh/1e6:.0f}M) → Institutional accumulation → LONG"
                )
                return self._create_signal(
                    self.symbol, "LONG", confidence, price,
                    sl_long(price, sl_pct), tp_long(price, tp_pct), rationale
                )

            elif etf_flow <= neg_thresh:
                magnitude = abs(etf_flow) / abs(neg_thresh)
                confidence = min(80.0, 45.0 + magnitude * 10.0)
                rationale = (
                    f"NEGATIVE ETF net flow: ${etf_flow/1e6:.1f}M "
                    f"(threshold ${neg_thresh/1e6:.0f}M) → Institutional distribution → SHORT"
                )
                return self._create_signal(
                    self.symbol, "SHORT", confidence, price,
                    sl_short(price, sl_pct), tp_short(price, tp_pct), rationale
                )

            return self._neutral(
                f"ETF flow ${etf_flow/1e6:.1f}M within normal range"
            )

        except Exception as exc:
            logger.error("EtfFlow[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
