"""
KAVACH-07 — Phase 2 Liquidity Flow Strategy & Order Book Manager
Detects 'Liquidity Walls' in the order book to identify support and resistance.
Includes the OrderBookManager for high-speed depth stream processing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import websockets
from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.liquidity_flow")

class OrderBookManager:
    """
    Maintains a real-time local cache of the top-of-book depth for all symbols.
    Subscribes to Binance @depth5@100ms streams.
    """

    def __init__(self, symbols: List[str], config: dict):
        self._symbols = symbols
        self._cfg = config
        self._books: Dict[str, Dict[str, List[List[float]]]] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Starts the depth stream WebSocket loop."""
        self._running = True
        self._task = asyncio.create_task(self._ws_loop())
        logger.info("OrderBookManager started for %d symbols", len(self._symbols))

    async def stop(self) -> None:
        """Stops the manager."""
        self._running = False
        if self._task:
            self._task.cancel()

    def get_book(self, symbol: str) -> Optional[Dict[str, List[List[float]]]]:
        """Returns the current snapshot of bids and asks for a symbol."""
        return self._books.get(symbol)

    async def _ws_loop(self) -> None:
        base_url = "wss://fstream.binance.com/stream?streams="
        # Using @depth5 (Top 5 levels) for efficiency on 1GB RAM
        streams = [f"{s.lower()}@depth5@100ms" for s in self._symbols]
        
        while self._running:
            try:
                async with websockets.connect(base_url + "/".join(streams)) as ws:
                    while self._running:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        stream_data = data.get("data", {})
                        symbol = stream_data.get("s")
                        if symbol:
                            self._books[symbol] = {
                                "bids": [[float(p), float(q)] for p, q in stream_data.get("b", [])],
                                "asks": [[float(p), float(q)] for p, q in stream_data.get("a", [])]
                            }
            except Exception as e:
                logger.warning("OrderBookManager WS error: %s. Reconnecting...", e)
                await asyncio.sleep(5)

class LiquidityFlow(StrategyBase):
    """
    Strategy logic using OrderBookManager data.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        ob_mgr: Optional[OrderBookManager] = data_context.get("order_book_manager")

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
            book = ob_mgr.get_book(self.symbol)
            if not book or not book.get('bids') or not book.get('asks'):
                return self._neutral("Empty order book")

            current_price = md.price
            bids = book['bids'] 
            asks = book['asks']

            support_wall = self._find_wall(bids, current_price, wall_mult, min_notional, proximity_limit, True)
            resistance_wall = self._find_wall(asks, current_price, wall_mult, min_notional, proximity_limit, False)

            side = "NEUTRAL"
            target_wall = None

            if support_wall and resistance_wall:
                if support_wall[2] > resistance_wall[2]:
                    side, target_wall = "LONG", support_wall
                else:
                    side, target_wall = "SHORT", resistance_wall
            elif support_wall:
                side, target_wall = "LONG", support_wall
            elif resistance_wall:
                side, target_wall = "SHORT", resistance_wall

            if side == "NEUTRAL" or not target_wall:
                return self._neutral("No liquidity walls detected")

            wall_price, wall_notional, intensity = target_wall
            conf = 65.0 + (intensity - wall_mult) * 2.0
            conf = max(65.0, min(92.0, conf))

            entry = current_price
            if side == "LONG":
                sl = min(entry * (1.0 - sl_pct), wall_price * 0.999)
                tp = entry * (1.0 + tp_pct)
            else:
                sl = max(entry * (1.0 + sl_pct), wall_price * 1.001)
                tp = entry * (1.0 - tp_pct)

            rationale = (
                f"Liquidity Flow: {'Support' if side == 'LONG' else 'Resistance'} at ${wall_price:.6g} "
                f"(${wall_notional/1e3:.1f}k, {intensity:.1f}x avg)."
            )

            return self._create_signal(
                side=side, confidence=conf, entry=entry, stop_loss=sl, take_profit=tp, rationale=rationale
            )

        except Exception as e:
            logger.error("LiquidityFlow error for %s: %s", self.symbol, e)
            return self._neutral(f"Error: {str(e)}")

    def _find_wall(self, levels, current_price, multiplier, min_notional, proximity_limit, is_bid):
        if not levels: return None
        avg_notional = sum(l[0] * l[1] for l in levels) / len(levels)
        best_wall = None
        for price, qty in levels:
            notional = price * qty
            dist = abs(price - current_price) / current_price
            if dist > proximity_limit: continue
            intensity = notional / avg_notional if avg_notional > 0 else 0
            if notional >= min_notional and intensity >= multiplier:
                if best_wall is None or notional > best_wall[1]:
                    best_wall = (price, notional, intensity)
        return best_wall