"""
KAVACH-07 Strategy: Tokenized Security Bias
Infer macro market risk-on/risk-off sentiment from tokenized traditional assets.
Uses simulated data (real data requires Mirror/Synthetix integration).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import Signal, StrategyBase
from ..core.indicators import sl_long, sl_short, tp_long, tp_short

logger = logging.getLogger(__name__)


class TokenizedSecurity(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        corr_thresh = float(self._cfg.get("correlation_threshold", 0.6))
        sl_pct      = float(self._cfg.get("sl_percent", 1.5))
        tp_pct      = float(self._cfg.get("tp_percent", 3.0))

        # Tokenized security sentiment from data_context (populated by DataEngine)
        tok_data = data_context.get("tokenized_securities")
        price    = md.price

        if tok_data is None:
            return self._neutral("Tokenized securities data unavailable")

        try:
            # tok_data = {"nasdaq_bias": float, "sp500_bias": float, "correlation": float}
            nasdaq_bias  = float(tok_data.get("nasdaq_bias", 0.0))   # +1=bullish, -1=bearish
            sp500_bias   = float(tok_data.get("sp500_bias", 0.0))
            correlation  = float(tok_data.get("correlation", 0.0))   # crypto-TradFi correlation

            if abs(correlation) < corr_thresh:
                return self._neutral(
                    f"TradFi-crypto correlation {correlation:.2f} < {corr_thresh} threshold"
                )

            composite_bias = (nasdaq_bias + sp500_bias) / 2.0
            if abs(composite_bias) < 0.3:
                return self._neutral(
                    f"Composite TradFi bias {composite_bias:.2f} too weak (< 0.3)"
                )

            magnitude  = abs(composite_bias)
            confidence = min(65.0, 30.0 + magnitude * 20.0 + abs(correlation) * 15.0)

            if composite_bias > 0:
                rationale = (
                    f"TRADFI RISK-ON: NASDAQ bias={nasdaq_bias:+.2f} SP500 bias={sp500_bias:+.2f} "
                    f"corr={correlation:.2f} → Risk-on crypto LONG"
                )
                return self._create_signal(
                    self.symbol, "LONG", confidence, price,
                    sl_long(price, sl_pct), tp_long(price, tp_pct), rationale
                )
            else:
                rationale = (
                    f"TRADFI RISK-OFF: NASDAQ bias={nasdaq_bias:+.2f} SP500 bias={sp500_bias:+.2f} "
                    f"corr={correlation:.2f} → Risk-off crypto SHORT"
                )
                return self._create_signal(
                    self.symbol, "SHORT", confidence, price,
                    sl_short(price, sl_pct), tp_short(price, tp_pct), rationale
                )

        except Exception as exc:
            logger.error("TokenizedSecurity[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
