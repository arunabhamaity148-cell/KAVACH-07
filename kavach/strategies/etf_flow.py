"""
KAVACH-07 — ETF Flow Strategy
Signals directional moves based on institutional capital flows into/out of Spot ETFs.
Note: Currently returns NEUTRAL until a real-time ETF data provider (e.g., Glassnode/CryptoQuant) 
is integrated into the DataEngine.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.etf_flow")

class EtfFlow(StrategyBase):
    """
    Logic:
    1. Monitor Net Institutional ETF Flow (USD).
    2. If Net Flow > Positive_Threshold: Institutional Accumulation -> LONG.
    3. If Net Flow < Negative_Threshold: Institutional Distribution -> SHORT.
    4. Only applicable to BTC and ETH symbols.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        """
        Processes ETF flow data. 
        Requirements: No fake proxies. Skip if data field is None.
        """
        # Logic Guard: ETF flows currently only track BTC and ETH assets
        if not (self.symbol.startswith("BTC") or self.symbol.startswith("ETH")):
            return self._neutral("ETF flow logic not applicable to this symbol")

        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        pos_thresh = float(self._cfg.get("positive_threshold", 50000000.0)) # $50M
        neg_thresh = float(self._cfg.get("negative_threshold", -50000000.0)) # -$50M
        sl_pct = float(self._cfg.get("sl_percent", 0.5)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 1.0)) / 100.0

        # Retrieve net flow from data context (Expected to be populated by DataEngine)
        # As per spec: If API unavailable -> data field = None
        net_flow_usd = data_context.get("etf_net_flow_usd")

        if net_flow_usd is None:
            return self._neutral("ETF flow data source currently disabled or unavailable")

        try:
            is_bullish = net_flow_usd >= pos_thresh
            is_bearish = net_flow_usd <= neg_thresh

            if not (is_bullish or is_bearish):
                return self._neutral(f"ETF Net Flow (${net_flow_usd/1e6:.1f}M) below institutional threshold")

            side = "LONG" if is_bullish else "SHORT"
            
            # Confidence scales with flow magnitude
            # Base 70% + 5% per $50M extra flow, capped at 95%
            excess = abs(net_flow_usd) - abs(pos_thresh if is_bullish else neg_thresh)
            conf = 70.0 + (excess / 50000000.0 * 5.0)
            conf = max(70.0, min(95.0, conf))

            entry = md.price
            if side == "LONG":
                sl = entry * (1.0 - sl_pct)
                tp = entry * (1.0 + tp_pct)
            else:
                sl = entry * (1.0 + sl_pct)
                tp = entry * (1.0 - tp_pct)

            rationale = (
                f"Significant Institutional {'Accumulation' if is_bullish else 'Distribution'} detected. "
                f"Net ETF Flow: ${net_flow_usd/1e6:.1f}M."
            )

            return self._create_signal(
                side=side,
                confidence=conf,
                entry=entry,
                stop_loss=sl,
                take_profit=tp,
                rationale=rationale,
                extra_metadata={
                    "net_flow_usd": net_flow_usd,
                    "symbol_mapped": self.symbol
                }
            )

        except Exception as e:
            logger.error("EtfFlow error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")