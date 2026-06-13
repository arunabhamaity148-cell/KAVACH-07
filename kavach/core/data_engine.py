"""
KAVACH-07 — Data Engine
Manages real-time data ingestion from Binance and Hyperliquid.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp
import numpy as np
import websockets

logger = logging.getLogger("kavach.data_engine")

class MarketData:
    """Memory-efficient storage for symbol market data."""
    __slots__ = (
        "symbol", "price", "mark_price", "volume", "quote_volume", 
        "open_interest", "funding_rate", "hl_price", "hl_funding",
        "klines_1m", "klines_5m", "agg_trades", "cvd", "vwap_num", "vwap_den",
        "last_update", "adx", "atr", "is_warm"
    )

    def __init__(self, symbol: str, hist_limit: int):
        self.symbol = symbol
        self.price: float = 0.0
        self.mark_price: float = 0.0
        self.volume: float = 0.0
        self.quote_volume: float = 0.0
        self.open_interest: float = 0.0
        self.funding_rate: float = 0.0
        self.hl_price: float = 0.0
        self.hl_funding: float = 0.0
        
        # Bounded deques for memory efficiency
        self.klines_1m: deque = deque(maxlen=hist_limit)
        self.klines_5m: deque = deque(maxlen=hist_limit)
        self.agg_trades: deque = deque(maxlen=1000)
        
        self.cvd: float = 0.0
        self.vwap_num: float = 0.0
        self.vwap_den: float = 0.0
        
        self.last_update: float = 0.0
        self.adx: float = 0.0
        self.atr: float = 0.0
        self.is_warm: bool = False

class DataEngine:
    """Ingests data from Binance (WS/REST) and Hyperliquid."""

    def __init__(self, config: dict):
        self._cfg = config
        self._symbols = self._cfg["trading"]["symbols"]["tier_s"] + \
                        self._cfg["trading"]["symbols"]["tier_a"] + \
                        self._cfg["trading"]["symbols"]["tier_b"]
        
        self._hist_limit = self._cfg["bot"]["historical_kline_limit"]
        self._data: Dict[str, MarketData] = {
            s: MarketData(s, self._hist_limit) for s in self._symbols
        }
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._last_vwap_reset = datetime.now(timezone.utc).day

    async def start(self) -> None:
        """Initialises session and starts background tasks."""
        self._session = aiohttp.ClientSession()
        self._running = True
        
        # Warmup: Fetch historical klines via REST
        await self._warmup_all()
        
        # Start Streams
        self._tasks.append(asyncio.create_task(self._binance_ws_loop()))
        self._tasks.append(asyncio.create_task(self._hyperliquid_poll_loop()))
        self._tasks.append(asyncio.create_task(self._binance_rest_poll_loop()))
        
        logger.info("Data Engine started for %d symbols", len(self._symbols))

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._session:
            await self._session.close()
        logger.info("Data Engine stopped")

    def get_market_data(self, symbol: str) -> Optional[MarketData]:
        """Accessor for market data snapshots."""
        return self._data.get(symbol)

    def is_healthy(self) -> bool:
        """Watchdog check for data staleness (120s)."""
        now = time.time()
        for md in self._data.values():
            if md.last_update > 0 and (now - md.last_update) > 120:
                logger.error("Watchdog: Symbol %s is stale (diff: %.1fs)", md.symbol, now - md.last_update)
                return False
        return True

    # ──────────────────────────────────────────────────────────────────────────
    # Ingestion Logic
    # ──────────────────────────────────────────────────────────────────────────

    async def _warmup_all(self) -> None:
        """Fetches initial klines to populate indicators."""
        logger.info("Warming up historical klines...")
        for symbol in self._symbols:
            await self._fetch_history(symbol, "5m", self._data[symbol].klines_5m)
            await self._fetch_history(symbol, "1m", self._data[symbol].klines_1m)
            self._update_indicators(self._data[symbol])
            self._data[symbol].is_warm = True
        logger.info("Warmup complete")

    async def _fetch_history(self, symbol: str, interval: str, target: deque) -> None:
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {"symbol": symbol, "interval": interval, "limit": self._hist_limit}
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for k in data:
                        target.append([float(x) for x in k[:6]]) # [time, o, h, l, c, v]
        except Exception as e:
            logger.error("History fetch failed for %s: %s", symbol, e)

    async def _binance_ws_loop(self) -> None:
        """Combined Binance WebSocket stream management."""
        base_url = "wss://fstream.binance.com/stream?streams="
        streams = []
        for s in self._symbols:
            sl = s.lower()
            streams.extend([
                f"{sl}@kline_1m", f"{sl}@kline_5m", f"{sl}@markPrice@1s", 
                f"{sl}@aggTrade", f"{sl}@depth5@100ms"
            ])
        streams.append("!forceOrder@arr") # Liquidations
        
        while self._running:
            try:
                async with websockets.connect(base_url + "/".join(streams)) as ws:
                    while self._running:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        self._process_ws_message(data)
            except Exception as e:
                logger.warning("Binance WS disconnected: %s. Reconnecting in 5s...", e)
                await asyncio.sleep(5)

    def _process_ws_message(self, msg: dict) -> None:
        """Routes stream data to appropriate MarketData objects."""
        stream = msg.get("stream", "")
        data = msg.get("data", {})
        
        if "!forceOrder" in stream:
            # Handle global liquidations if needed
            return

        symbol = data.get("s")
        if not symbol or symbol not in self._data:
            return
            
        md = self._data[symbol]
        md.last_update = time.time()
        
        if "@kline" in stream:
            k = data["k"]
            interval = k["i"]
            k_data = [float(k["t"]), float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"])]
            if interval == "1m":
                md.klines_1m.append(k_data)
            elif interval == "5m":
                md.klines_5m.append(k_data)
                if k["x"]: # Candle closed
                    self._update_indicators(md)
            
        elif "@markPrice" in stream:
            md.mark_price = float(data["p"])
            md.funding_rate = float(data["r"])
            
        elif "@aggTrade" in stream:
            md.price = float(data["p"])
            vol = float(data["q"])
            # CVD Calculation: Taker side
            # m=True means buyer was maker (taker sell). m=False means buyer was taker (taker buy).
            md.cvd += vol if not data["m"] else -vol
            self._update_vwap(md, md.price, vol)

    def _update_vwap(self, md: MarketData, price: float, volume: float) -> None:
        """Calculates daily VWAP, resets on new UTC day."""
        current_day = datetime.now(timezone.utc).day
        if current_day != self._last_vwap_reset:
            md.vwap_num = 0.0
            md.vwap_den = 0.0
            md.cvd = 0.0 # Reset CVD daily alongside VWAP
            self._last_vwap_reset = current_day
            
        typical_price = price # Using last price as simple typical price for aggTrades
        md.vwap_num += typical_price * volume
        md.vwap_den += volume

    def _update_indicators(self, md: MarketData) -> None:
        """Computes technical indicators from kline data."""
        if len(md.klines_5m) < 30:
            return
            
        # Convert to numpy for faster math
        klines = np.array(list(md.klines_5m))
        highs = klines[:, 2]
        lows = klines[:, 3]
        closes = klines[:, 4]
        
        # ATR(14)
        tr = np.maximum(highs[1:] - lows[1:], 
                        np.maximum(abs(highs[1:] - closes[:-1]), 
                                   abs(lows[1:] - closes[:-1])))
        md.atr = float(np.mean(tr[-14:]))
        
        # ADX(14) - Simplified robust version
        up_move = highs[1:] - highs[:-1]
        down_move = lows[:-1] - lows[1:]
        
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        
        # Smoothed values
        s_tr = np.mean(tr[-14:])
        s_pdm = np.mean(plus_dm[-14:])
        s_mdm = np.mean(minus_dm[-14:])
        
        if s_tr > 0:
            plus_di = 100 * (s_pdm / s_tr)
            minus_di = 100 * (s_mdm / s_tr)
            dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di + 1e-9)
            md.adx = dx # Use DX as ADX for current snapshot
            
    async def _hyperliquid_poll_loop(self) -> None:
        """Polls Hyperliquid info for price and funding parity."""
        url = "https://api.hyperliquid.xyz/info"
        while self._running:
            try:
                # Map Binance symbol to HL asset name
                # Hyperliquid typically uses BTC, ETH etc. (stripping USDT)
                payload = {"type": "metaAndAssetCtxs"}
                async with self._session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        meta, ctxs = data[0], data[1]
                        universe = meta["universe"]
                        for i, asset_meta in enumerate(universe):
                            name = asset_meta["name"]
                            # Matching: e.g., BTC -> BTCUSDT
                            target_sym = f"{name}USDT"
                            if target_sym in self._data:
                                md = self._data[target_sym]
                                md.hl_price = float(ctxs[i]["markPx"])
                                md.hl_funding = float(ctxs[i]["funding"])
            except Exception as e:
                logger.warning("Hyperliquid poll failed: %s", e)
            await asyncio.sleep(5)

    async def _binance_rest_poll_loop(self) -> None:
        """Polls REST endpoints for slower-moving data (OI)."""
        while self._running:
            for symbol in self._symbols:
                if not self._running: break
                url = "https://fapi.binance.com/fapi/v1/openInterest"
                try:
                    async with self._session.get(url, params={"symbol": symbol}) as resp:
                        if resp.status == 200:
                            res = await resp.json()
                            self._data[symbol].open_interest = float(res["openInterest"])
                except Exception:
                    pass
                await asyncio.sleep(2) # Throttle to avoid rate limits
            await asyncio.sleep(20)