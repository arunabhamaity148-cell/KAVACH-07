"""
KAVACH-07 — Hyperliquid Lead-Lag Strategy
Arbitrage-style strategy that detects price leads on Hyperliquid L1 
relative to Binance Futures.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.hyperliquid_leadlag")

class HyperliquidLeadlag(StrategyBase):
    """
    Logic:
    1. Compare real-time price on Hyperliquid (HL) vs Binance (BNB).
    2. Calculate percentage divergence.
    3. If HL leads BNB by > threshold, generate signal in direction of leader.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        # Threshold: 0.0015 (0.15%)
        threshold = float(self._cfg.get("divergence_threshold", 0.0015))
        sl_pct = float(self._cfg.get("sl_percent", 0.1)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 0.3)) / 100.0

        binance_price = md.price
        hl_price = md.hl_price

        # Validate inputs
        if binance_price <= 0 or hl_price <= 0:
            return self._neutral("Insufficient price data from either exchange")

        try:
            # Formula: (DEX - CEX) / CEX
            divergence = (hl_price - binance_price) / binance_price
            abs_div = abs(divergence)

            if abs_div < threshold:
                return self._neutral(f"Divergence {divergence*100:.4f}% below threshold")

            # Determine Side
            # If HL > BNB: HL leads UP, BNB follows -> LONG
            # If HL < BNB: HL leads DOWN, BNB follows -> SHORT
            side = "LONG" if divergence > 0 else "SHORT"

            # Confidence scales with how much the threshold is exceeded
            # Base 65% + 10% for every 0.1% beyond threshold, capped at 92%
            excess = abs_div - threshold
            conf = 65.0 + (excess * 1000.0 * 10.0)
            conf = max(65.0, min(92.0, conf))

            entry = binance_price
            if side == "LONG":
                sl = entry * (1.0 - sl_pct)
                tp = entry * (1.0 + tp_pct)
            else:
                sl = entry * (1.0 + sl_pct)
                tp = entry * (1.0 - tp_pct)

            rationale = (
                f"Price lead detected on Hyperliquid. Divergence: {divergence*100:.3f}%. "
                f"Hyperliquid price ${hl_price:.6g} vs Binance ${binance_price:.6g}."
            )

            return self._create_signal(
                side=side,
                confidence=conf,
                entry=entry,
                stop_loss=sl,
                take_profit=tp,
                rationale=rationale,
                extra_metadata={
                    "divergence_pct": round(divergence * 100, 4),
                    "hl_price": hl_price,
                    "bn_price": binance_price
                }
            )

        except Exception as e:
            logger.error("HyperliquidLeadlag error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")