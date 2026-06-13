"""
KAVACH-07 — Data Engine (REMEDIATED)
High-frequency data ingestion engine for Binance Futures and Hyperliquid.
Implements Wilder's ADX/ATR, Daily VWAP, and True Cumulative Volume Delta (CVD).
Includes 120s staleness watchdog and memory-efficient slot-based storage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import numpy as np
import websockets

logger = logging.getLogger("kavach.data_engine")

class MarketData:
    """
    Memory-efficient container for a single symbol's market state.
    Uses __slots__ to minimize RAM overhead on OCPU/1GB VPS instances.
    """
    __slots__ = (
        "symbol", "last_price", "mark_price", "index_price", "volume_24h",
        "open_interest", "funding_rate", "hl_price", "hl_funding",
        "last_update_ts", "klines_1m", "klines_5m", "liq_history",
        "cvd_cumulative", "vwap", "adx", "atr", "plus_di", "minus_di",
        "_vwap_sum_pv", "_vwap_sum_v", "_last_vwap_reset_day"
    )

    def __init__(self, symbol: str, kline_limit: int = 200):
        self.symbol = symbol
        self.last_price: float = 0.0
        self.mark_price: float = 0.0
        self.index_price: float = 0.0
        self.volume_24h: float = 0.0
        self.open_interest: float = 0.0
        self.funding_rate: float = 0.0
        
        self.hl_price: float = 0.0
        self.hl_funding: float = 0.0
        
        self.last_update_ts: float = 0.0
        
        # Bounded deques for performance and memory predictability
        self.klines_1m: deque[Tuple[float, ...]] = deque(maxlen=kline_limit)
        self.klines_5m: deque[Tuple[float, ...]] = deque(maxlen=kline_limit)
        self.liq_history: deque[Dict[str, Any]] = deque(maxlen=100)
        
        # Core Indicators
        self.cvd_cumulative: float = 0.0
        self.vwap: float = 0.0
        self.adx: float = 0.0
        self.atr: float = 0.0
        self.plus_di: float = 0.0
        self.minus_di: float = 0.0
        
        # Calculation State
        self._vwap_sum_pv: float = 0.0
        self._vwap_sum_v: float = 0.0
        self._last_vwap_reset_day: int = datetime.now(timezone.utc).day

class DataEngine:
    """
    Multi-exchange data aggregator. 
    Handles WebSocket streams, REST polling, and real-time indicator math.
    """

    def __init__(self, config: dict):
        self._cfg = config
        self._kline_limit = int(config["bot"]["historical_kline_limit"])
        
        # Symbol list aggregation from tiers
        self._symbols: List[str] = (
            config["trading"]["symbols"]["tier_s"] +
            config["trading"]["symbols"]["tier_a"] +
            config["trading"]["symbols"]["tier_b"]
        )
        
        self._data: Dict[str, MarketData] = {
            s: MarketData(s, self._kline_limit) for s in self._symbols
        }
        
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._tasks: List[asyncio.Task] = []

    async def start(self) -> None:
        """Starts all background ingestion and processing tasks."""
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self._running = True
        
        # 1. Warmup: Populate buffers via REST so indicators work from tick 1
        await self._warmup_historical_data()
        
        # 2. Start Live Streams
        self._tasks.append(asyncio.create_task(self._binance_ws_loop()))
        
        # 3. Start Metric Polls (OI, Volume, HL Parity)
        self._tasks.append(asyncio.create_task(self._poll_rest_metrics()))
        self._tasks.append(asyncio.create_task(self._poll_hyperliquid()))
        
        logger.info("DataEngine ONLINE: Tracking %d assets", len(self._symbols))

    async def stop(self) -> None:
        """Graceful shutdown sequence."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._session:
            await self._session.close()
        logger.info("DataEngine OFFLINE")

    def get_market_data(self, symbol: str) -> Optional[MarketData]:
        """Provides access to the current state of a symbol."""
        return self._data.get(symbol)

    def is_healthy(self) -> bool:
        """
        WATCHDOG: Logic gate to prevent trading on stale or disconnected data.
        Returns False if any symbol hasn't updated in 120 seconds.
        """
        now = time.time()
        for s, md in self._data.items():
            if md.last_update_ts > 0:
                gap = now - md.last_update_ts
                if gap > 120:
                    logger.critical(f"WATCHDOG FAILURE: {s} data is stale ({gap:.1f}s). HALTING.")
                    return False
        return True

    # ──────────────────────────────────────────────────────────────────────────
    # WebSocket Logic (Binance)
    # ──────────────────────────────────────────────────────────────────────────

    async def _binance_ws_loop(self) -> None:
        """Combined stream loop for Binance Futures."""
        url_base = "wss://fstream.binance.com/stream?streams="
        
        streams = []
        for s in self._symbols:
            sl = s.lower()
            streams.extend([
                f"{sl}@kline_1m", f"{sl}@kline_5m", 
                f"{sl}@markPrice@1s", f"{sl}@aggTrade"
            ])
        # Add global liquidation stream
        streams.append("!forceOrder@arr")
        
        full_url = url_base + "/".join(streams)
        
        while self._running:
            try:
                async with websockets.connect(full_url) as ws:
                    logger.info("Binance WebSocket: Connection established")
                    while self._running:
                        raw = await ws.recv()
                        msg = json.loads(raw)
                        await self._handle_ws_message(msg)
            except Exception as e:
                logger.error(f"Binance WS Error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _handle_ws_message(self, msg: dict) -> None:
        """Main dispatcher for incoming stream data."""
        stream = msg.get("stream", "")
        data = msg.get("data", {})
        
        if "!forceOrder" in stream:
            self._process_liquidation(data)
            return

        symbol = data.get("s")
        if not symbol or symbol not in self._data:
            return
        
        md = self._data[symbol]
        md.last_update_ts = time.time()
        
        # Kline / Candlestick Logic
        if "@kline" in stream:
            k = data["k"]
            k_tuple = (
                float(k["t"]), float(k["o"]), float(k["h"]), 
                float(k["l"]), float(k["c"]), float(k["v"])
            )
            if k["i"] == "1m":
                md.klines_1m.append(k_tuple)
            elif k["i"] == "5m":
                md.klines_5m.append(k_tuple)
                if k["x"]: # Recalculate indicators ONLY when candle closes
                    await self._calculate_indicators(md)

        # Mark Price & Funding Logic
        elif "@markPrice" in stream:
            md.mark_price = float(data.get("p", 0))
            md.index_price = float(data.get("i", 0))
            md.funding_rate = float(data.get("r", 0))

        # Real-time Trade Logic (CVD & VWAP)
        elif "@aggTrade" in stream:
            price = float(data["p"])
            qty = float(data["q"])
            is_taker_sell = data["m"] # m=True means buyer was maker -> Taker Sell
            
            md.last_price = price
            
            # 1. Daily Reset Check (UTC)
            now_day = datetime.now(timezone.utc).day
            if now_day != md._last_vwap_reset_day:
                md._vwap_sum_pv = 0.0
                md._vwap_sum_v = 0.0
                md.cvd_cumulative = 0.0
                md._last_vwap_reset_day = now_day
                logger.info(f"RESET: Daily metrics for {md.symbol}")
            
            # 2. Update VWAP
            md._vwap_sum_pv += price * qty
            md._vwap_sum_v += qty
            md.vwap = md._vwap_sum_pv / md._vwap_sum_v if md._vwap_sum_v > 0 else price
            
            # 3. Update CVD (Centralized for all strategies)
            # Delta = Quantity if Taker Buy, -Quantity if Taker Sell
            md.cvd_cumulative += (-qty if is_taker_sell else qty)

    def _process_liquidation(self, data: dict) -> None:
        """Handles real-time liquidation data for momentum strategies."""
        try:
            o = data.get("o", {})
            symbol = o.get("s")
            if symbol in self._data:
                self._data[symbol].liq_history.append({
                    "ts": time.time(),
                    "side": o.get("S"), # BUY/SELL
                    "price": float(o.get("p", 0)),
                    "qty": float(o.get("q", 0)),
                    "usd_size": float(o.get("p", 0)) * float(o.get("q", 0))
                })
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────────
    # Polling & Warmup Logic
    # ──────────────────────────────────────────────────────────────────────────

    async def _warmup_historical_data(self) -> None:
        """REST warmup to ensure bot can trade immediately on startup."""
        logger.info(f"WARMUP: Fetching {self._kline_limit} bars for {len(self._symbols)} symbols")
        tasks = [self._fetch_klines(s, "5m") for s in self._symbols]
        await asyncio.gather(*tasks)
        
        for md in self._data.values():
            if len(md.klines_5m) >= 30:
                await self._calculate_indicators(md)
        logger.info("WARMUP: Complete. Math engines active.")

    async def _fetch_klines(self, symbol: str, interval: str) -> None:
        """REST client for historical bars."""
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {"symbol": symbol, "interval": interval, "limit": self._kline_limit}
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    raw = await resp.json()
                    md = self._data[symbol]
                    for k in raw:
                        k_tuple = (float(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
                        if interval == "5m":
                            md.klines_5m.append(k_tuple)
                        else:
                            md.klines_1m.append(k_tuple)
        except Exception as e:
            logger.error(f"WARMUP ERROR ({symbol}): {e}")

    async def _poll_rest_metrics(self) -> None:
        """Polls Binance for Open Interest and 24h ticker data."""
        while self._running:
            try:
                # 24h Rolling Volume
                async with self._session.get("https://fapi.binance.com/fapi/v1/ticker/24hr") as resp:
                    if resp.status == 200:
                        results = await resp.json()
                        for res in results:
                            s = res["symbol"]
                            if s in self._data:
                                self._data[s].volume_24h = float(res["quoteVolume"])

                # Open Interest (Iterative polling with delay to stay under rate limits)
                for s in self._symbols:
                    if not self._running: break
                    url = "https://fapi.binance.com/fapi/v1/openInterest"
                    async with self._session.get(url, params={"symbol": s}) as resp:
                        if resp.status == 200:
                            res = await resp.json()
                            self._data[s].open_interest = float(res["openInterest"])
                    await asyncio.sleep(0.5) 
                
            except Exception as e:
                logger.error(f"POLL ERROR (Binance REST): {e}")
            await asyncio.sleep(30)

    async def _poll_hyperliquid(self) -> None:
        """Polls Hyperliquid L1 for cross-exchange arbitrage parity."""
        url = "https://api.hyperliquid.xyz/info"
        while self._running:
            try:
                payload = {"type": "metaAndAssetCtxs"}
                async with self._session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        raw = await resp.json()
                        meta, ctxs = raw[0], raw[1]
                        universe = meta["universe"]
                        for i, asset in enumerate(universe):
                            bn_sym = f"{asset['name']}USDT"
                            if bn_sym in self._data:
                                self._data[bn_sym].hl_price = float(ctxs[i]["markPx"])
                                self._data[bn_sym].hl_funding = float(ctxs[i]["funding"])
            except Exception as e:
                logger.debug(f"POLL ERROR (Hyperliquid): {e}")
            await asyncio.sleep(5)

    # ──────────────────────────────────────────────────────────────────────────
    # High-Integrity Mathematical Indicators
    # ──────────────────────────────────────────────────────────────────────────

    async def _calculate_indicators(self, md: MarketData) -> None:
        """
        Pure NumPy implementation of Wilder's original smoothing formulas.
        Calculates ATR(14) and ADX(14) with zero drift.
        """
        if len(md.klines_5m) < 30:
            return
            
        # 1. Prepare Data
        arr = np.array(list(md.klines_5m), dtype=np.float64)
        h, l, c = arr[:, 2], arr[:, 3], arr[:, 4]
        
        # 2. Calculate True Range (TR)
        tr = np.maximum(h[1:] - l[1:], 
                        np.maximum(np.abs(h[1:] - c[:-1]), 
                                   np.abs(l[1:] - c[:-1])))
        
        # Wilder's Smoothing Function
        # S_i = (S_{i-1} * (n-1) + Current) / n
        def smooth_wilder(series, period):
            res = np.zeros_like(series)
            if len(series) < period: return res
            # First value is simple SMA
            res[period-1] = np.mean(series[:period])
            # Following values use Wilder's EMA
            for i in range(period, len(series)):
                res[i] = (res[i-1] * (period - 1) + series[i]) / period
            return res

        # 3. ATR (14) Calculation
        atr_series = smooth_wilder(tr, 14)
        md.atr = float(atr_series[-1])
        
        # 4. ADX (14) Calculation
        up_move = h[1:] - h[:-1]
        down_move = l[:-1] - l[1:]
        
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        
        tr_s = smooth_wilder(tr, 14)
        pdm_s = smooth_wilder(plus_dm, 14)
        mdm_s = smooth_wilder(minus_dm, 14)
        
        # Avoid division by zero
        eps = 1e-10
        p_di = 100 * (pdm_s / (tr_s + eps))
        m_di = 100 * (mdm_s / (tr_s + eps))
        
        dx = 100 * np.abs(p_di - m_di) / (p_di + m_di + eps)
        # ADX is the 14-period Wilder smoothing of DX
        adx_series = smooth_wilder(dx[13:], 14) # Start from where DX is valid
        
        md.plus_di = float(p_di[-1])
        md.minus_di = float(m_di[-1])
        md.adx = float(adx_series[-1]) if len(adx_series) > 0 else 0.0