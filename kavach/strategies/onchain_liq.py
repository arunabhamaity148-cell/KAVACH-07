"""
KAVACH-07 — On-Chain Liquidation Strategy
Detects proximity to large liquidation clusters to anticipate bounces or cascades.
Requirement: No fake proxies. If API data is None, returns NEUTRAL.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.onchain_liq")

class OnchainLiq(StrategyBase):
    """
    Logic:
    1. Monitor large liquidation clusters (Levels where many traders get liquidated).
    2. Proximity check: If price is within X% of a cluster.
    3. If price approaches a SHORT cluster from below: Resistance/Continuation -> SHORT/LONG.
    4. If price approaches a LONG cluster from above: Support/Continuation -> LONG/SHORT.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        # proximity_threshold: 0.005 (0.5%)
        # min_cluster_size: 5,000,000 USD
        proximity = float(self._cfg.get("proximity_threshold", 0.005))
        min_size = float(self._cfg.get("min_cluster_size", 5000000.0))
        sl_pct = float(self._cfg.get("sl_percent", 1.0)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 2.0)) / 100.0

        # Retrieve cluster data from context (Expected from DataEngine via Coinglass API)
        # Structure: list of {"price": float, "size_usd": float, "side": "LONG"|"SHORT"}
        clusters: Optional[List[Dict[str, Any]]] = data_context.get(f"{self.symbol}_liq_clusters")

        if clusters is None:
            return self._neutral("Liquidation cluster data unavailable or provider disabled")

        try:
            current_price = md.price
            valid_clusters = [c for c in clusters if c["size_usd"] >= min_size]
            
            if not valid_clusters:
                return self._neutral(f"No clusters above ${min_size/1e6:.1f}M found")

            # Find nearest clusters above and below
            nearest_above = None
            nearest_below = None
            
            for c in valid_clusters:
                dist = (c["price"] - current_price) / current_price
                if dist > 0: # Above
                    if nearest_above is None or dist < (nearest_above["price"] - current_price) / current_price:
                        nearest_above = c
                else: # Below
                    dist = abs(dist)
                    if nearest_below is None or dist < abs((nearest_below["price"] - current_price) / current_price):
                        nearest_below = c

            # Check proximity and determine side
            # Approaching SHORT cluster (High leverage shorts will be forced to buy) -> Continuation UP (LONG)
            if nearest_above and nearest_above["side"] == "SHORT":
                dist_pct = (nearest_above["price"] - current_price) / current_price
                if dist_pct <= proximity:
                    return self._create_liq_signal("LONG", dist_pct, proximity, nearest_above, md.price, sl_pct, tp_pct)

            # Approaching LONG cluster (High leverage longs will be forced to sell) -> Continuation DOWN (SHORT)
            if nearest_below and nearest_below["side"] == "LONG":
                dist_pct = (current_price - nearest_below["price"]) / current_price
                if dist_pct <= proximity:
                    return self._create_liq_signal("SHORT", dist_pct, proximity, nearest_below, md.price, sl_pct, tp_pct)

            return self._neutral("No clusters within proximity threshold")

        except Exception as e:
            logger.error("OnchainLiq error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")

    def _create_liq_signal(
        self, 
        side: str, 
        dist_pct: float, 
        limit_pct: float, 
        cluster: Dict[str, Any], 
        entry: float,
        sl_pct: float,
        tp_pct: float
    ) -> Signal:
        # Confidence scales with proximity and cluster size
        # Proximity score: 1.0 at 0 dist, 0.0 at limit
        prox_score = (limit_pct - dist_pct) / limit_pct
        conf = 60.0 + (prox_score * 30.0)
        conf = max(60.0, min(95.0, conf))

        if side == "LONG":
            sl = entry * (1.0 - sl_pct)
            tp = entry * (1.0 + tp_pct)
        else:
            sl = entry * (1.0 + sl_pct)
            tp = entry * (1.0 - tp_pct)

        rationale = (
            f"Approaching large {cluster['side']} liquidation cluster (${cluster['size_usd']/1e6:.1f}M) "
            f"at {cluster['price']:.6g}. Proximity: {dist_pct*100:.2f}%. Anticipating cascade/continuation."
        )

        return self._create_signal(
            side=side,
            confidence=conf,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            rationale=rationale,
            extra_metadata={
                "cluster_price": cluster["price"],
                "cluster_size": cluster["size_usd"],
                "dist_pct": round(dist_pct * 100, 4)
            }
        )