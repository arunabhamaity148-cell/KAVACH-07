"""
KAVACH-07 — Stablecoin Flow Strategy
Signals buying pressure or capital exit based on net on-chain stablecoin movements.
Requirement: No fake proxies. If API data is None, returns NEUTRAL.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.stablecoin_flow")

class StablecoinFlow(StrategyBase):
    """
    Logic:
    1. Monitor Net Stablecoin Flow (Inflow to exchanges - Outflow).
    2. If Net Flow > min_flow_value: Capital entering markets -> Bullish -> LONG.
    3. If Net Flow < -min_flow_value: Capital exiting to cold storage -> Bearish -> SHORT.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        # Threshold: 30,000,000 USD
        min_flow = float(self._cfg.get("min_flow_value", 30000000.0))
        sl_pct = float(self._cfg.get("sl_percent", 0.5)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 1.0)) / 100.0

        # Retrieve flow data from context (Expected from DataEngine or External API)
        # As per spec: data field = None if API unavailable
        net_stable_flow_usd = data_context.get("net_stablecoin_flow_usd")

        if net_stable_flow_usd is None:
            return self._neutral("Stablecoin flow data unavailable or provider disabled")

        try:
            is_bullish = net_stable_flow_usd >= min_flow
            is_bearish = net_stable_flow_usd <= -min_flow

            if not (is_bullish or is_bearish):
                return self._neutral(
                    f"Stablecoin flow (${net_stable_flow_usd/1e6:.1f}M) below threshold"
                )

            side = "LONG" if is_bullish else "SHORT"

            # Confidence scales with flow magnitude relative to threshold
            # Base 65% + 10% per multiplier of threshold, capped at 90%
            magnitude = abs(net_stable_flow_usd) / min_flow
            conf = 65.0 + (magnitude - 1.0) * 10.0
            conf = max(65.0, min(90.0, conf))

            entry = md.price
            if side == "LONG":
                sl = entry * (1.0 - sl_pct)
                tp = entry * (1.0 + tp_pct)
            else:
                sl = entry * (1.0 + sl_pct)
                tp = entry * (1.0 - tp_pct)

            rationale = (
                f"On-chain Stablecoin {'Inflow' if is_bullish else 'Outflow'} detected. "
                f"Net Flow: ${net_stable_flow_usd/1e6:.1f}M. Direction: {side}."
            )

            return self._create_signal(
                side=side,
                confidence=conf,
                entry=entry,
                stop_loss=sl,
                take_profit=tp,
                rationale=rationale,
                extra_metadata={
                    "net_flow_usd": net_stable_flow_usd,
                    "flow_magnitude": round(magnitude, 2)
                }
            )

        except Exception as e:
            logger.error("StablecoinFlow error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")