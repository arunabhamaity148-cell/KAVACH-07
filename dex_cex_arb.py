"""
KAVACH-07 Strategy: DEX-CEX Arbitrage
Exploit funding rate spread between Hyperliquid (DEX) and Binance (CEX).
Signals the opportunity — directional bias follows the funding differential.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import Signal, StrategyBase
from ..core.indicators import sl_long, sl_short, tp_long, tp_short

logger = logging.getLogger(__name__)


class DexCexArb(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        min_diff = float(self._cfg.get("min_funding_diff_threshold", 0.0003))
        sl_pct   = float(self._cfg.get("sl_percent", 0.5))
        tp_pct   = float(self._cfg.get("tp_percent", 0.8))

        binance_fr = md.funding_rate
        hl_fr      = md.hyperliquid_funding
        price      = md.price

        if hl_fr == 0.0 and binance_fr == 0.0:
            return self._neutral("Both funding rates unavailable")

        try:
            diff = hl_fr - binance_fr  # positive = HL has higher funding (longs pay more on HL)

            if abs(diff) < min_diff:
                return self._neutral(
                    f"Funding diff {diff*100:.4f}% < min {min_diff*100:.4f}%"
                )

            magnitude = abs(diff) / min_diff
            confidence = min(65.0, 35.0 + magnitude * 8.0)

            if diff > 0:
                # HL funding > Binance: Longs overpaying on HL → mean-revert
                # Directional: SHORT on HL, LONG on Binance (here we signal LONG Binance)
                rationale = (
                    f"DEX-CEX ARB: HL funding {hl_fr*100:.4f}% > BNB {binance_fr*100:.4f}% "
                    f"(diff={diff*100:.4f}%) → LONG Binance (cheaper funding)"
                )
                return self._create_signal(
                    self.symbol, "LONG", confidence, price,
                    sl_long(price, sl_pct), tp_long(price, tp_pct), rationale
                )
            else:
                # Binance funding > HL: Longs overpaying on Binance
                rationale = (
                    f"DEX-CEX ARB: BNB funding {binance_fr*100:.4f}% > HL {hl_fr*100:.4f}% "
                    f"(diff={abs(diff)*100:.4f}%) → SHORT Binance (cheaper to fund on HL)"
                )
                return self._create_signal(
                    self.symbol, "SHORT", confidence, price,
                    sl_short(price, sl_pct), tp_short(price, tp_pct), rationale
                )

        except Exception as exc:
            logger.error("DexCexArb[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
