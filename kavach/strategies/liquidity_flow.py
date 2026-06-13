"""
KAVACH-07 — Phase 2 Liquidity Flow Strategy
Detects 'Liquidity Walls' in the order book (large limit orders) to identify 
strong support and resistance levels.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.liquidity_flow")

class LiquidityFlow(StrategyBase):
    """
    Logic:
    1. Access real-time order book depth via OrderBookManager (passed in data_context).
    2. Identify 'Walls':
       - Price level where Notional (Price * Qty) >= min_wall_notional (default $500k).
       - Size must be >= wall_size_multiplier (default 5x) the average level size.
    3. Proximity Check: Wall must be within price_proximity_percent (default 1%) of current price.
    4. Directional Bias:
       - Large Bid Wall below price = Support -> LONG.
       - Large Ask Wall above price = Resistance -> SHORT.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        ob_mgr = data_context.get("order_book_manager")

        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")
        
        if not ob_mgr:
            return self._neutral("OrderBookManager not available in context")

        # Config parameters
        wall_mult = float(self._cfg.get("wall_size_multiplier", 5.0))
        min_notional = float(self._cfg.get("min_wall_notional", 500000.0))
        proximity_limit = float(self._cfg.get("price_proximity_percent", 1.0)) / 100.0
        
        sl_pct = float(self._cfg.get("sl_percent", 0.5)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 1.0)) / 100.0

        try:
            # 1. Retrieve Depth Snapshot
            # Expected structure from ob_mgr.get_book(symbol): 
            # {'bids': [[price, qty], ...], 'asks': [[price, qty], ...]}
            book = ob_mgr.get_book(self.symbol)
            if not book or not book.get('bids') or not book.get('asks'):
                return self._neutral("Empty order book")

            current_price = md.price
            bids = book['bids'] # List of [price, qty]
            asks = book['asks']

            # 2. Analyze Bids (Support Walls)
            support_wall = self._find_wall(bids, current_price, wall_mult, min_notional, proximity_limit, is_bid=True)
            
            # 3. Analyze Asks (Resistance Walls)
            resistance_wall = self._find_wall(asks, current_price, wall_mult, min_notional, proximity_limit, is_bid=False)

            # 4. Signal Logic
            # Priority: We take the wall with the highest relative size (intensity)
            side = "NEUTRAL"
            target_wall = None

            if support_wall and resistance_wall:
                # Conflict: Take the larger one
                if support_wall[2] > resistance_wall[2]:
                    side, target_wall = "LONG", support_wall
                else:
                    side, target_wall = "SHORT", resistance_wall
            elif support_wall:
                side, target_wall = "LONG", support_wall
            elif resistance_wall:
                side, target_wall = "SHORT", resistance_wall

            if side == "NEUTRAL" or not target_wall:
                return self._neutral("No significant liquidity walls detected within range")

            # 5. Calculate Parameters
            wall_price, wall_notional, intensity = target_wall
            
            # Confidence scales with intensity (5x -> 65%, 20x -> 90%)
            conf = 65.0 + (intensity - wall_mult) * 2.0
            conf = max(65.0, min(92.0, conf))

            entry = current_price
            if side == "LONG":
                # Entry at current price, SL behind the wall
                sl = min(entry * (1.0 - sl_pct), wall_price * 0.999)
                tp = entry * (1.0 + tp_pct)
            else:
                # Entry at current price, SL behind the wall
                sl = max(entry * (1.0 + sl_pct), wall_price * 1.001)
                tp = entry * (1.0 - tp_pct)

            rationale = (
                f"Liquidity Flow: Large {'Support' if side == 'LONG' else 'Resistance'} wall detected. "
                f"Price: ${wall_price:.6g}, Notional: ${wall_notional/1e3:.1f}k ({intensity:.1f}x avg). "
                f"Proximity: {abs(wall_price - entry)/entry*100:.2f}%."
            )

            return self._create_signal(
                side=side,
                confidence=conf,
                entry=entry,
                stop_loss=sl,
                take_profit=tp,
                rationale=rationale,
                extra_metadata={
                    "wall_price": wall_price,
                    "wall_notional": round(wall_notional, 2),
                    "intensity": round(intensity, 2),
                    "side": "BID" if side == "LONG" else "ASK"
                }
            )

        except Exception as e:
            logger.error("LiquidityFlow error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")

    def _find_wall(
        self, 
        levels: List[List[float]], 
        current_price: float, 
        multiplier: float, 
        min_notional: float, 
        proximity_limit: float,
        is_bid: bool
    ) -> Optional[Tuple[float, float, float]]:
        """
        Scans order book levels to find the most significant wall.
        Returns: (price, notional, intensity) or None
        """
        if not levels:
            return None

        # Calculate average level size for intensity baseline
        # Use first 20 levels
        sample_levels = levels[:20]
        avg_notional = sum(float(l[0]) * float(l[1]) for l in sample_levels) / len(sample_levels)
        
        best_wall = None
        
        for price, qty in levels:
            price, qty = float(price), float(qty)
            notional = price * qty
            
            # Distance check
            dist = abs(price - current_price) / current_price
            if dist > proximity_limit:
                continue
                
            # Intensity check
            intensity = notional / avg_notional if avg_notional > 0 else 0
            
            # Wall criteria
            if notional >= min_notional and intensity >= multiplier:
                # If multiple walls, pick the one with highest notional
                if best_wall is None or notional > best_wall[1]:
                    best_wall = (price, notional, intensity)
                    
        return best_wall