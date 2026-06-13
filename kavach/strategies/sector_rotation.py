"""
KAVACH-07 — Sector Rotation Strategy
Signals directional moves based on 24h Relative Strength (RS) within specific 
market sectors (Defi, AI, Meme, L1, L2).
Requirement: Exclude FUD pairs from baskets and relative strength calculation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np
from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.sector_rotation")

class SectorRotation(StrategyBase):
    """
    Logic:
    1. Define market sectors (L1, L2, Defi, AI, Memes).
    2. Calculate 24h percentage change for the current symbol.
    3. Calculate the average 24h percentage change for its sector (excluding FUD).
    4. Relative Strength (RS) = Symbol_Change - Sector_Average_Change.
    5. Signal LONG if RS > 2%, SHORT if RS < -2%.
    """

    # Sector definitions (Production baseline)
    SECTORS = {
        "LAYER_1": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "NEARUSDT"],
        "LAYER_2": ["ARBUSDT", "OPUSDT", "MATICUSDT", "STXUSDT", "MNTUSDT", "METISUSDT"],
        "DEFI": ["AAVEUSDT", "UNIUSDT", "MKRUSDT", "LDOUSDT", "PENDLEUSDT", "CRVUSDT"],
        "AI": ["FETUSDT", "RNDRUSDT", "AGIXUSDT", "OCEANUSDT", "WLDUSDT", "TAOUSDT"],
        "MEME": ["DOGEUSDT", "SHIBUSDT", "PEPEUSDT", "WIFUSDT", "FLOKIUSDT", "BONKUSDT"]
    }

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        threshold = float(self._cfg.get("relative_strength_threshold", 0.02))
        sl_pct = float(self._cfg.get("sl_percent", 1.0)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 2.0)) / 100.0

        # Retrieve FUD list from context or config to exclude from calculation
        fud_list = self.config.get("risk", {}).get("regulatory_fud_pairs", [])
        # Normalize fud list
        normalized_fud = {s if s.endswith("USDT") else s + "USDT" for s in fud_list}

        try:
            # 1. Identify Symbol's Sector
            sector_name = self._get_sector_name(self.symbol)
            if not sector_name:
                return self._neutral(f"Symbol {self.symbol} not assigned to a sector")

            # 2. Calculate Symbol's 24h Change
            # 24h = 1440 minutes. We check if we have enough 1m kline data.
            if len(md.klines_1m) < 1440:
                # Fallback to whatever history is available if not fully 24h, but warn
                if len(md.klines_1m) < 60: # Minimum 1h
                    return self._neutral("Insufficient 1m history for RS calculation")
            
            symbol_change = self._calculate_24h_change(md)

            # 3. Calculate Sector Average Change (Excluding FUD and self)
            sector_peers = self.SECTORS[sector_name]
            peer_changes: List[float] = []

            for peer in sector_peers:
                if peer == self.symbol or peer in normalized_fud:
                    continue
                
                peer_md = data_context.get(peer)
                if peer_md and peer_md.is_warm and len(peer_md.klines_1m) >= 60:
                    peer_changes.append(self._calculate_24h_change(peer_md))

            if not peer_changes:
                return self._neutral(f"No valid peers found in sector {sector_name}")

            sector_avg = float(np.mean(peer_changes))

            # 4. Calculate Relative Strength
            # Formula: RS = Symbol % - Sector %
            relative_strength = symbol_change - sector_avg

            if abs(relative_strength) < threshold:
                return self._neutral(f"RS ({relative_strength*100:.2f}%) within {threshold*100}% threshold")

            # 5. Determine Side
            side = "LONG" if relative_strength > 0 else "SHORT"
            
            # Confidence scales with RS magnitude
            # 2% RS -> 65% Conf, 5% RS -> 90% Conf
            conf = 65.0 + (abs(relative_strength) - threshold) * 800.0
            conf = max(65.0, min(95.0, conf))

            entry = md.price
            if side == "LONG":
                sl = entry * (1.0 - sl_pct)
                tp = entry * (1.0 + tp_pct)
            else:
                sl = entry * (1.0 + sl_pct)
                tp = entry * (1.0 - tp_pct)

            rationale = (
                f"Sector Rotation: {self.symbol} is {'outperforming' if side == 'LONG' else 'underperforming'} "
                f"the {sector_name} sector. 24h Change: {symbol_change*100:.2f}% vs "
                f"Sector Avg: {sector_avg*100:.2f}% (RS: {relative_strength*100:.2f}%)."
            )

            return self._create_signal(
                side=side,
                confidence=conf,
                entry=entry,
                stop_loss=sl,
                take_profit=tp,
                rationale=rationale,
                extra_metadata={
                    "sector": sector_name,
                    "symbol_24h_change": round(symbol_change, 4),
                    "sector_24h_avg_change": round(sector_avg, 4),
                    "relative_strength": round(relative_strength, 4)
                }
            )

        except Exception as e:
            logger.error("SectorRotation error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")

    def _get_sector_name(self, symbol: str) -> Optional[str]:
        """Finds which sector a symbol belongs to."""
        for name, members in self.SECTORS.items():
            if symbol in members:
                return name
        return None

    def _calculate_24h_change(self, md: Any) -> float:
        """Calculates percentage change over available history up to 24h."""
        klines = list(md.klines_1m)
        current_price = md.price
        # If we have 1440 klines, we use index 0. If less, we use index 0.
        open_price = klines[0][1] # Open of the oldest candle in the buffer
        if open_price <= 0:
            return 0.0
        return (current_price - open_price) / open_price