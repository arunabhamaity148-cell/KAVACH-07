"""
KAVACH-07 Strategy: Market Maker PnL
Infer MM unrealized PnL via open interest + price displacement from entry estimate.
Extreme MM loss → forced de-risk/squeeze → directional signal.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np

from .base import Signal, StrategyBase
from ..core.indicators import klines_to_ohlcv, sma_last, sl_long, sl_short, tp_long, tp_short

logger = logging.getLogger(__name__)


class MarketMakerPnl(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        pnl_thresh = float(self._cfg.get("pnl_divergence_threshold_percent", 15.0))
        sl_pct     = float(self._cfg.get("sl_percent", 1.5))
        tp_pct     = float(self._cfg.get("tp_percent", 3.0))

        price = md.price

        try:
            # Direct MM PnL data if available (e.g., from Coinglass or on-chain)
            mm_data = data_context.get(f"{self.symbol}_mm_pnl")

            if mm_data is not None:
                mm_pnl_pct = float(mm_data.get("unrealized_pnl_percent", 0.0))
            else:
                # ── Proxy: Estimate MM net exposure via OI vs price displacement ──
                klines = md.kline_data
                if len(klines) < 40:
                    return self._neutral("Insufficient kline history for MM PnL proxy")

                o, h, l, c, v = klines_to_ohlcv(klines)

                # Estimate "average entry" of OI holders over last 20 bars
                # Weighted by volume: higher volume = more positions opened at that price
                window = 20
                c_w = c[-window:]
                v_w = v[-window:]
                total_vol = np.sum(v_w) + 1e-10
                avg_entry = float(np.sum(c_w * v_w) / total_vol)

                if avg_entry <= 0:
                    return self._neutral("Cannot estimate MM average entry")

                # MM PnL proxy: if OI is net long (positive CVD), then OI holders are net long
                cvd = md.cvd
                oi  = md.open_interest

                # Direction: CVD > 0 = net long positioning
                net_direction = 1.0 if cvd >= 0 else -1.0
                mm_pnl_pct = net_direction * (price - avg_entry) / avg_entry * 100.0

            if abs(mm_pnl_pct) < pnl_thresh:
                return self._neutral(
                    f"MM PnL proxy {mm_pnl_pct:.1f}% < ±{pnl_thresh:.1f}% threshold"
                )

            magnitude  = abs(mm_pnl_pct) / pnl_thresh
            confidence = min(68.0, 35.0 + magnitude * 10.0)

            if mm_pnl_pct < -pnl_thresh:
                # MMs in significant LOSS → forced de-risk / squeeze → LONG
                rationale = (
                    f"MM PnL PROXY: {mm_pnl_pct:.1f}% loss → MMs under pressure "
                    f"→ Potential forced buy-back/squeeze → LONG"
                )
                return self._create_signal(
                    self.symbol, "LONG", confidence, price,
                    sl_long(price, sl_pct), tp_long(price, tp_pct), rationale
                )
            else:
                # MMs in significant GAIN → profit taking likely → SHORT
                rationale = (
                    f"MM PnL PROXY: +{mm_pnl_pct:.1f}% gain → MMs likely taking profits "
                    f"→ Potential distribution/reversal → SHORT"
                )
                return self._create_signal(
                    self.symbol, "SHORT", confidence, price,
                    sl_short(price, sl_pct), tp_short(price, tp_pct), rationale
                )

        except Exception as exc:
            logger.error("MarketMakerPnl[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
