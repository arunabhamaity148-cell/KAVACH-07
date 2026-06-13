"""
KAVACH-07 Strategy: Hyperliquid Lead/Lag
When Hyperliquid price diverges from Binance, anticipate Binance following.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import Signal, StrategyBase
from ..core.indicators import sl_long, sl_short, tp_long, tp_short

logger = logging.getLogger(__name__)


class HyperliquidLeadlag(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        div_thresh = float(self._cfg.get("divergence_threshold_percent", 0.15)) / 100.0
        sl_pct     = float(self._cfg.get("sl_percent", 0.4))
        tp_pct     = float(self._cfg.get("tp_percent", 0.8))

        binance_px = md.price
        hl_px      = md.hyperliquid_price

        if hl_px <= 0:
            return self._neutral("Hyperliquid price unavailable")

        try:
            divergence = (hl_px - binance_px) / binance_px  # positive = HL higher

            if abs(divergence) < div_thresh:
                return self._neutral(
                    f"HL-BNB divergence {divergence*100:.3f}% < threshold {div_thresh*100:.3f}%"
                )

            magnitude = abs(divergence) / div_thresh  # how many multiples of threshold
            confidence = min(88.0, 50.0 + magnitude * 15.0)

            if divergence > 0:
                # HL > Binance → Binance likely to catch up → LONG Binance
                rationale = (
                    f"Hyperliquid LEADS Binance UP: HL={hl_px:.4f} BNB={binance_px:.4f} "
                    f"div={divergence*100:.3f}% → Binance lag-follow LONG"
                )
                return self._create_signal(
                    self.symbol, "LONG", confidence, binance_px,
                    sl_long(binance_px, sl_pct), tp_long(binance_px, tp_pct), rationale
                )
            else:
                # HL < Binance → Binance likely to fall → SHORT Binance
                rationale = (
                    f"Hyperliquid LEADS Binance DOWN: HL={hl_px:.4f} BNB={binance_px:.4f} "
                    f"div={divergence*100:.3f}% → Binance lag-follow SHORT"
                )
                return self._create_signal(
                    self.symbol, "SHORT", confidence, binance_px,
                    sl_short(binance_px, sl_pct), tp_short(binance_px, tp_pct), rationale
                )

        except Exception as exc:
            logger.error("HyperliquidLeadlag[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
