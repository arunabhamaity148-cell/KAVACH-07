"""
KAVACH-07 Strategy: Sector Rotation
Identify outperforming sectors and trade relative strength within them.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import numpy as np

from .base import Signal, StrategyBase
from ..core.indicators import sl_long, sl_short, tp_long, tp_short

logger = logging.getLogger(__name__)


class SectorRotation(StrategyBase):
    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        rs_thresh  = float(self._cfg.get("relative_strength_threshold", 0.02))
        sl_pct     = float(self._cfg.get("sl_percent", 1.5))
        tp_pct     = float(self._cfg.get("tp_percent", 3.0))
        sectors    = self._cfg.get("sectors", {})
        lookback   = int(self._cfg.get("lookback_hours", 24)) * 60  # convert to bars (approx)

        try:
            # Find which sector this symbol belongs to
            symbol_sector = None
            sector_peers  = []
            clean_sym = self.symbol.replace("USDT", "").replace("BUSD", "")
            for sector_name, members in sectors.items():
                for m in members:
                    if m == self.symbol or m.replace("USDT","") == clean_sym:
                        symbol_sector = sector_name
                        sector_peers  = members
                        break
                if symbol_sector:
                    break

            if symbol_sector is None:
                return self._neutral(f"{self.symbol} not in any defined sector")

            # Calculate symbol's 24h change using kline data
            klines = md.kline_data
            if len(klines) < 10:
                return self._neutral("Insufficient kline history for sector analysis")

            kline_list = list(klines)
            lookback_idx = max(0, len(kline_list) - min(lookback, len(kline_list)))
            open_price_24h = float(kline_list[lookback_idx][0])  # open of 24h-ago bar
            current_price  = md.price
            symbol_change  = (current_price - open_price_24h) / (open_price_24h + 1e-10)

            # Calculate average sector change (using available peer data)
            peer_changes: List[float] = []
            for peer_sym in sector_peers:
                if peer_sym == self.symbol:
                    continue
                peer_md = data_context.get(peer_sym)
                if peer_md is None or len(peer_md.kline_data) < 10:
                    continue
                peer_klines = list(peer_md.kline_data)
                peer_idx = max(0, len(peer_klines) - min(lookback, len(peer_klines)))
                peer_open = float(peer_klines[peer_idx][0])
                peer_change = (peer_md.price - peer_open) / (peer_open + 1e-10)
                peer_changes.append(peer_change)

            # Calculate overall market change (BTC as proxy)
            btc_md = data_context.get("BTCUSDT")
            market_change = 0.0
            if btc_md and len(btc_md.kline_data) >= 10:
                btc_klines = list(btc_md.kline_data)
                btc_idx = max(0, len(btc_klines) - min(lookback, len(btc_klines)))
                btc_open = float(btc_klines[btc_idx][0])
                market_change = (btc_md.price - btc_open) / (btc_open + 1e-10)

            sector_avg = float(np.mean(peer_changes)) if peer_changes else market_change
            relative_strength = symbol_change - sector_avg

            if relative_strength > rs_thresh:
                # Symbol outperforming sector → LONG
                sector_outperf = sector_avg - market_change
                momentum_score = relative_strength / rs_thresh
                confidence = min(75.0, 40.0 + momentum_score * 12.0)
                if sector_outperf > 0:
                    confidence = min(80.0, confidence + 8.0)  # sector also outperforming market

                rationale = (
                    f"Sector={symbol_sector} OUTPERFORMER: {self.symbol} "
                    f"+{symbol_change*100:.1f}% vs sector avg {sector_avg*100:.1f}% "
                    f"(RS={relative_strength*100:.2f}%)"
                )
                return self._create_signal(
                    self.symbol, "LONG", confidence, current_price,
                    sl_long(current_price, sl_pct), tp_long(current_price, tp_pct), rationale
                )

            elif relative_strength < -rs_thresh:
                # Symbol underperforming sector → SHORT
                momentum_score = abs(relative_strength) / rs_thresh
                confidence = min(75.0, 40.0 + momentum_score * 12.0)
                rationale = (
                    f"Sector={symbol_sector} UNDERPERFORMER: {self.symbol} "
                    f"{symbol_change*100:.1f}% vs sector avg {sector_avg*100:.1f}% "
                    f"(RS={relative_strength*100:.2f}%)"
                )
                return self._create_signal(
                    self.symbol, "SHORT", confidence, current_price,
                    sl_short(current_price, sl_pct), tp_short(current_price, tp_pct), rationale
                )

            return self._neutral(
                f"Sector={symbol_sector} | RS={relative_strength*100:.2f}% | "
                f"Below threshold ±{rs_thresh*100:.1f}%"
            )

        except Exception as exc:
            logger.error("SectorRotation[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
