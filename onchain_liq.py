"""
KAVACH-07 Strategy: On-Chain Liquidation Cluster
Price approaching large liquidation cluster → anticipate cascade or bounce.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import Signal, StrategyBase
from ..core.indicators import sl_long, sl_short, tp_long, tp_short

logger = logging.getLogger(__name__)


class OnchainLiq(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        proximity_pct  = float(self._cfg.get("price_proximity_threshold_percent", 0.5)) / 100.0
        min_cluster_sz = float(self._cfg.get("min_cluster_size_usd", 5_000_000))
        sl_pct         = float(self._cfg.get("sl_percent", 0.8))
        tp_pct         = float(self._cfg.get("tp_percent", 1.8))

        price = md.price
        lc    = md.liquidation_clusters

        try:
            long_price  = float(lc.get("long_cluster_price", 0.0))
            long_size   = float(lc.get("long_cluster_size", 0.0))
            short_price = float(lc.get("short_cluster_price", 0.0))
            short_size  = float(lc.get("short_cluster_size", 0.0))

            signals_found = []

            # ── Large short liquidation cluster → price approaching → potential squeeze → LONG
            if short_price > 0 and short_size >= min_cluster_sz:
                dist_to_short = abs(price - short_price) / price
                if dist_to_short <= proximity_pct:
                    proximity_score = (proximity_pct - dist_to_short) / proximity_pct
                    size_score = min(1.0, short_size / (min_cluster_sz * 5.0))
                    confidence = min(90.0, 50.0 + proximity_score * 25.0 + size_score * 20.0)
                    rationale = (
                        f"SHORT LIQ CLUSTER near: ${short_size/1e6:.1f}M at {short_price:.4f} "
                        f"({dist_to_short*100:.2f}% away) → Short squeeze potential → LONG"
                    )
                    signals_found.append(("LONG", confidence, rationale))

            # ── Large long liquidation cluster → price approaching → potential cascade → SHORT
            if long_price > 0 and long_size >= min_cluster_sz:
                dist_to_long = abs(price - long_price) / price
                if dist_to_long <= proximity_pct:
                    proximity_score = (proximity_pct - dist_to_long) / proximity_pct
                    size_score = min(1.0, long_size / (min_cluster_sz * 5.0))
                    confidence = min(90.0, 50.0 + proximity_score * 25.0 + size_score * 20.0)
                    rationale = (
                        f"LONG LIQ CLUSTER near: ${long_size/1e6:.1f}M at {long_price:.4f} "
                        f"({dist_to_long*100:.2f}% away) → Long liquidation cascade risk → SHORT"
                    )
                    signals_found.append(("SHORT", confidence, rationale))

            if not signals_found:
                return self._neutral("No significant liq clusters in proximity")

            # If conflicting signals, take the stronger one
            signals_found.sort(key=lambda x: x[1], reverse=True)
            side, conf, rat = signals_found[0]

            if side == "LONG":
                return self._create_signal(
                    self.symbol, side, conf, price,
                    sl_long(price, sl_pct), tp_long(price, tp_pct), rat
                )
            else:
                return self._create_signal(
                    self.symbol, side, conf, price,
                    sl_short(price, sl_pct), tp_short(price, tp_pct), rat
                )

        except Exception as exc:
            logger.error("OnchainLiq[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
