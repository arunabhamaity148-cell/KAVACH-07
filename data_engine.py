"""
KAVACH-07 — Data Engine
Manages Binance Futures WebSocket streams and REST polling.
Maintains live MarketData for all active symbols.
Calculates ADX, ATR, VWAP, CVD on each kline close.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set

import aiohttp

from .indicators import adx, atr, cvd_from_klines, vwap_from_klines

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# MarketData Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class MarketData:
    symbol: str
    price: float
    volume: float
    quote_volume: float
    timestamp: float = field(default_factory=time.time)
    open_interest: float = 0.0
    funding_rate: float = 0.0
    adx: float = 0.0
    atr: float = 0.0
    vwap: float = 0.0
    cvd: float = 0.0
    kline_data: Deque = field(default_factory=lambda: deque(maxlen=200))
    spot_volume: float = 0.0
    hyperliquid_price: float = 0.0
    hyperliquid_funding: float = 0.0
    fng_index: int = 50
    etf_net_flow: float = 0.0
    stablecoin_net_flow: float = 0.0
    liquidation_clusters: Dict[str, float] = field(default_factory=lambda: {
        "long_cluster_price": 0.0,
        "long_cluster_size": 0.0,
        "short_cluster_price": 0.0,
        "short_cluster_size": 0.0,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Rate Limiter
# ─────────────────────────────────────────────────────────────────────────────

class RateLimiter:
    """Token-bucket rate limiter for REST API calls."""

    def __init__(self, max_per_minute: int = 1200) -> None:
        self._max   = max_per_minute
        self._used  = 0
        self._reset = time.monotonic() + 60.0
        self._lock  = asyncio.Lock()

    async def acquire(self, weight: int = 1) -> None:
        async with self._lock:
            now = time.monotonic()
            if now >= self._reset:
                self._used  = 0
                self._reset = now + 60.0
            if self._used + weight > self._max:
                sleep_for = self._reset - now
                logger.debug("Rate limit: sleeping %.2fs", sleep_for)
                await asyncio.sleep(max(0.0, sleep_for))
                self._used  = 0
                self._reset = time.monotonic() + 60.0
            self._used += weight


# ─────────────────────────────────────────────────────────────────────────────
# Data Engine
# ─────────────────────────────────────────────────────────────────────────────

class DataEngine:
    """Async data ingestion engine for KAVACH-07.

    Responsibilities:
    - Manage Binance Futures WebSocket streams (grouped ≤20 per connection)
    - REST-poll Open Interest, Funding Rates, Spot Volume
    - REST-poll external data (F&G, Hyperliquid, liquidation clusters)
    - Calculate ADX / ATR / VWAP / CVD on each kline close
    - Expose get_data_context() for strategy consumption
    """

    def __init__(self, config: dict, db_manager: Any) -> None:
        self._cfg       = config
        self._db        = db_manager
        self._bcfg      = config.get("binance", {})
        self._dcfg      = config.get("data_engine", {})
        self._ws_cfg    = config.get("websocket", {})
        self._ext_cfg   = config.get("external_apis", {})

        self._symbols: List[str] = self._build_symbol_list()
        self._market_data: Dict[str, MarketData] = {}
        self._oi_history: Dict[str, List[float]] = {s: [] for s in self._symbols}
        self._liq_events: Dict[str, Optional[Dict]] = {s: None for s in self._symbols}
        self._tokenized_securities: Optional[Dict] = None

        self._rate_limiter = RateLimiter(
            int(self._dcfg.get("rate_limit_requests_per_minute", 1200))
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self._tasks: List[asyncio.Task] = []
        self._running = False
        self._api_health: Dict[str, bool] = {
            "binance_ws": False, "binance_rest": False,
            "hyperliquid": False, "external": False,
        }

        # Initialise MarketData stubs
        for sym in self._symbols:
            self._market_data[sym] = MarketData(symbol=sym, price=0.0,
                                                volume=0.0, quote_volume=0.0)

    # ─────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start all data ingestion tasks."""
        self._running = True
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        )
        logger.info("DataEngine: fetching historical klines for warm-up…")
        await self._fetch_all_historical()
        logger.info("DataEngine: starting live streams…")
        self._tasks = [
            asyncio.create_task(self._run_kline_streams(),      name="kline_ws"),
            asyncio.create_task(self._run_markprice_streams(),  name="markprice_ws"),
            asyncio.create_task(self._run_aggtrade_streams(),   name="aggtrade_ws"),
            asyncio.create_task(self._poll_open_interest(),     name="oi_poll"),
            asyncio.create_task(self._poll_spot_volume(),       name="spot_poll"),
            asyncio.create_task(self._poll_external_data(),     name="ext_poll"),
            asyncio.create_task(self._poll_hyperliquid(),       name="hl_poll"),
            asyncio.create_task(self._flush_db_loop(),          name="db_flush"),
        ]
        logger.info("DataEngine started. Tracking %d symbols.", len(self._symbols))

    async def stop(self) -> None:
        """Cancel all tasks and close HTTP session."""
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("DataEngine stopped.")

    # ─────────────────────────────────────────────────────────────────────
    # Public accessors
    # ─────────────────────────────────────────────────────────────────────

    def get_data_context(self) -> Dict[str, Any]:
        """Return unified data context dict for strategy consumption."""
        ctx: Dict[str, Any] = {}
        for sym in self._symbols:
            ctx[sym] = self._market_data[sym]
            ctx[f"{sym}_oi_history"]  = list(self._oi_history.get(sym, []))
            ctx[f"{sym}_liq_event"]   = self._liq_events.get(sym)
        if self._tokenized_securities:
            ctx["tokenized_securities"] = self._tokenized_securities
        return ctx

    @property
    def api_health(self) -> Dict[str, bool]:
        return dict(self._api_health)

    @property
    def symbols(self) -> List[str]:
        return list(self._symbols)

    # ─────────────────────────────────────────────────────────────────────
    # Symbol list builder
    # ─────────────────────────────────────────────────────────────────────

    def _build_symbol_list(self) -> List[str]:
        pairs_cfg = self._cfg.get("pairs", {})
        syms: List[str] = (
            list(pairs_cfg.get("tier_s", [])) +
            list(pairs_cfg.get("tier_a", [])) +
            list(pairs_cfg.get("tier_b", []))
        )
        # Remove regulatory FUD pairs
        fud = set(self._cfg.get("risk", {}).get("regulatory_fud_pairs", []))
        return [s for s in syms if s not in fud]

    # ─────────────────────────────────────────────────────────────────────
    # Historical klines (warm-up)
    # ─────────────────────────────────────────────────────────────────────

    async def _fetch_all_historical(self) -> None:
        limit = int(self._dcfg.get("historical_kline_limit", 200))
        intervals = self._dcfg.get("kline_intervals", ["1m", "5m", "15m"])
        primary_interval = intervals[1] if len(intervals) > 1 else intervals[0]  # 5m
        tasks = [
            self._fetch_historical_klines(sym, primary_interval, limit)
            for sym in self._symbols
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sym, result in zip(self._symbols, results):
            if isinstance(result, Exception):
                logger.warning("Historical fetch failed for %s: %s", sym, result)

    async def _fetch_historical_klines(
        self, symbol: str, interval: str, limit: int
    ) -> None:
        url = f"{self._bcfg.get('futures_rest_url', 'https://fapi.binance.com')}/fapi/v1/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        await self._rate_limiter.acquire(weight=5)
        try:
            async with self._session.get(url, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
            md = self._market_data[symbol]
            md.kline_data.clear()
            for k in data:
                # Binance kline: [openTime, open, high, low, close, volume, ...]
                md.kline_data.append((
                    float(k[1]), float(k[2]), float(k[3]),
                    float(k[4]), float(k[5]),
                ))
            self._recalc_indicators(symbol)
            self._api_health["binance_rest"] = True
            logger.debug("Historical klines loaded for %s (%d bars)", symbol, len(md.kline_data))
        except Exception as exc:
            logger.warning("_fetch_historical_klines(%s): %s", symbol, exc)
            self._api_health["binance_rest"] = False

    # ─────────────────────────────────────────────────────────────────────
    # WebSocket: Klines
    # ─────────────────────────────────────────────────────────────────────

    async def _run_kline_streams(self) -> None:
        """Connect WebSocket for 5m kline streams, grouped ≤20 per connection."""
        import websockets
        intervals = self._dcfg.get("kline_intervals", ["1m", "5m", "15m"])
        # Primary interval for indicator calculation = 5m
        primary_interval = intervals[1] if len(intervals) > 1 else intervals[0]
        streams = [f"{s.lower()}@kline_{primary_interval}" for s in self._symbols]
        await self._ws_stream_loop(streams, self._handle_kline_msg, "kline")

    async def _run_markprice_streams(self) -> None:
        streams = [f"{s.lower()}@markPrice@1s" for s in self._symbols]
        await self._ws_stream_loop(streams, self._handle_markprice_msg, "markprice")

    async def _run_aggtrade_streams(self) -> None:
        streams = [f"{s.lower()}@aggTrade" for s in self._symbols]
        await self._ws_stream_loop(streams, self._handle_aggtrade_msg, "aggtrade")

    async def _ws_stream_loop(
        self,
        streams: List[str],
        handler,
        label: str,
    ) -> None:
        """Manage WebSocket connections with auto-reconnect."""
        import websockets

        max_per_conn  = int(self._ws_cfg.get("streams_per_connection", 20))
        reconnect_del = float(self._ws_cfg.get("reconnect_delay_seconds", 5))
        max_retries   = int(self._ws_cfg.get("max_reconnect_attempts", 10))
        ping_interval = int(self._ws_cfg.get("ping_interval_seconds", 20))
        base_ws_url   = self._bcfg.get("futures_ws_url", "wss://fstream.binance.com/stream")

        # Split streams into groups
        groups = [streams[i: i + max_per_conn] for i in range(0, len(streams), max_per_conn)]

        async def _connect_group(group: List[str]) -> None:
            stream_path = "/".join(group)
            url = f"{base_ws_url}?streams={stream_path}"
            retries = 0
            while self._running and retries < max_retries:
                try:
                    async with websockets.connect(
                        url,
                        ping_interval=ping_interval,
                        ping_timeout=30,
                        close_timeout=10,
                    ) as ws:
                        retries = 0
                        self._api_health["binance_ws"] = True
                        logger.info("WS[%s] connected: %d streams", label, len(group))
                        async for raw in ws:
                            if not self._running:
                                return
                            try:
                                msg = json.loads(raw)
                                data = msg.get("data", msg)
                                await handler(data)
                            except Exception as parse_exc:
                                logger.debug("WS[%s] parse error: %s", label, parse_exc)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    retries += 1
                    self._api_health["binance_ws"] = False
                    logger.warning(
                        "WS[%s] disconnected (attempt %d/%d): %s",
                        label, retries, max_retries, exc,
                    )
                    await asyncio.sleep(reconnect_del * min(retries, 5))
            logger.error("WS[%s] max retries reached — giving up.", label)

        await asyncio.gather(*[_connect_group(g) for g in groups])

    # ─────────────────────────────────────────────────────────────────────
    # WebSocket message handlers
    # ─────────────────────────────────────────────────────────────────────

    async def _handle_kline_msg(self, data: dict) -> None:
        try:
            k   = data.get("k", {})
            sym = k.get("s", "")
            if sym not in self._market_data:
                return
            md = self._market_data[sym]
            is_closed = k.get("x", False)
            o, h, l, c, v = (
                float(k["o"]), float(k["h"]),
                float(k["l"]), float(k["c"]), float(k["v"]),
            )
            # Always update latest price
            md.price        = c
            md.volume       = v
            md.quote_volume = float(k.get("q", 0.0))
            md.timestamp    = time.time()

            if is_closed:
                # Append closed bar and recalculate indicators
                md.kline_data.append((o, h, l, c, v))
                self._recalc_indicators(sym)
        except (KeyError, ValueError) as exc:
            logger.debug("_handle_kline_msg error: %s", exc)

    async def _handle_markprice_msg(self, data: dict) -> None:
        try:
            sym = data.get("s", "")
            if sym not in self._market_data:
                return
            md = self._market_data[sym]
            md.price        = float(data.get("p", md.price))
            md.funding_rate = float(data.get("r", md.funding_rate))
            md.timestamp    = time.time()
        except (KeyError, ValueError) as exc:
            logger.debug("_handle_markprice_msg error: %s", exc)

    async def _handle_aggtrade_msg(self, data: dict) -> None:
        try:
            sym = data.get("s", "")
            if sym not in self._market_data:
                return
            md = self._market_data[sym]
            md.price     = float(data.get("p", md.price))
            md.volume    = float(data.get("q", md.volume))
            md.timestamp = time.time()
        except (KeyError, ValueError) as exc:
            logger.debug("_handle_aggtrade_msg error: %s", exc)

    # ─────────────────────────────────────────────────────────────────────
    # Indicator calculation
    # ─────────────────────────────────────────────────────────────────────

    def _recalc_indicators(self, symbol: str) -> None:
        """Recalculate ADX, ATR, VWAP, CVD from kline_data deque."""
        md = self._market_data.get(symbol)
        if md is None or len(md.kline_data) < 15:
            return
        try:
            import numpy as np
            kl = list(md.kline_data)
            o  = np.array([float(k[0]) for k in kl], dtype=float)
            h  = np.array([float(k[1]) for k in kl], dtype=float)
            l  = np.array([float(k[2]) for k in kl], dtype=float)
            c  = np.array([float(k[3]) for k in kl], dtype=float)
            v  = np.array([float(k[4]) for k in kl], dtype=float)

            adx_val, pdi, mdi = adx(h, l, c, period=14)
            atr_val           = atr(h, l, c, period=14)
            vwap_val          = vwap_from_klines(md.kline_data)
            cvd_val           = cvd_from_klines(md.kline_data)

            md.adx  = adx_val
            md.atr  = atr_val
            md.vwap = vwap_val
            md.cvd  = cvd_val
        except Exception as exc:
            logger.debug("_recalc_indicators(%s): %s", symbol, exc)

    # ─────────────────────────────────────────────────────────────────────
    # REST polling: Open Interest
    # ─────────────────────────────────────────────────────────────────────

    async def _poll_open_interest(self) -> None:
        interval = int(self._dcfg.get("oi_poll_interval_seconds", 30))
        base_url = self._bcfg.get("futures_rest_url", "https://fapi.binance.com")
        while self._running:
            try:
                tasks = [self._fetch_oi_single(sym, base_url) for sym in self._symbols]
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("OI poll error: %s", exc)
            await asyncio.sleep(interval)

    async def _fetch_oi_single(self, symbol: str, base_url: str) -> None:
        url = f"{base_url}/fapi/v1/openInterest"
        await self._rate_limiter.acquire(weight=1)
        try:
            async with self._session.get(url, params={"symbol": symbol}) as resp:
                resp.raise_for_status()
                data = await resp.json()
            oi = float(data.get("openInterest", 0.0))
            self._market_data[symbol].open_interest = oi
            hist = self._oi_history.setdefault(symbol, [])
            hist.append(oi)
            if len(hist) > 200:
                hist.pop(0)
        except Exception as exc:
            logger.debug("_fetch_oi_single(%s): %s", symbol, exc)

    # ─────────────────────────────────────────────────────────────────────
    # REST polling: Spot Volume
    # ─────────────────────────────────────────────────────────────────────

    async def _poll_spot_volume(self) -> None:
        interval = int(self._dcfg.get("oi_poll_interval_seconds", 30))
        spot_url = self._bcfg.get("spot_rest_url", "https://api.binance.com")
        while self._running:
            try:
                await self._rate_limiter.acquire(weight=1)
                url = f"{spot_url}/api/v3/ticker/24hr"
                async with self._session.get(url) as resp:
                    resp.raise_for_status()
                    tickers = await resp.json()
                sym_map = {t["symbol"]: float(t.get("quoteVolume", 0.0)) for t in tickers}
                for sym in self._symbols:
                    if sym in sym_map:
                        self._market_data[sym].spot_volume = sym_map[sym]
                self._api_health["binance_rest"] = True
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("Spot volume poll error: %s", exc)
                self._api_health["binance_rest"] = False
            await asyncio.sleep(interval * 2)

    # ─────────────────────────────────────────────────────────────────────
    # REST polling: Hyperliquid
    # ─────────────────────────────────────────────────────────────────────

    async def _poll_hyperliquid(self) -> None:
        interval = int(self._dcfg.get("hyperliquid_poll_interval_seconds", 10))
        url      = self._ext_cfg.get("hyperliquid_info_url", "https://api.hyperliquid.xyz/info")
        symbol_map = {
            "BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL",
            "BNBUSDT": "BNB", "XRPUSDT": "XRP", "DOGEUSDT": "DOGE",
            "ADAUSDT": "ADA", "AVAXUSDT": "AVAX", "LINKUSDT": "LINK",
            "ARBUSDT": "ARB",
        }
        while self._running:
            try:
                payload = {"type": "metaAndAssetCtxs"}
                await self._rate_limiter.acquire(weight=1)
                async with self._session.post(url, json=payload) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

                # Response: [meta, assetCtxs]
                if isinstance(data, list) and len(data) == 2:
                    meta       = data[0]
                    asset_ctxs = data[1]
                    universe   = meta.get("universe", [])
                    for i, asset_info in enumerate(universe):
                        hl_name = asset_info.get("name", "")
                        if i < len(asset_ctxs):
                            ctx = asset_ctxs[i]
                            hl_price   = float(ctx.get("markPx", 0.0))
                            hl_funding = float(ctx.get("funding", 0.0))
                            # Map HL asset name → USDT symbol
                            for binance_sym, hl_sym in symbol_map.items():
                                if hl_sym == hl_name and binance_sym in self._market_data:
                                    self._market_data[binance_sym].hyperliquid_price   = hl_price
                                    self._market_data[binance_sym].hyperliquid_funding = hl_funding
                self._api_health["hyperliquid"] = True
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug("Hyperliquid poll error: %s", exc)
                self._api_health["hyperliquid"] = False
                # Fallback: copy Binance price as proxy
                for sym in self._symbols:
                    if self._market_data[sym].hyperliquid_price == 0.0:
                        self._market_data[sym].hyperliquid_price = self._market_data[sym].price
            await asyncio.sleep(interval)

    # ─────────────────────────────────────────────────────────────────────
    # REST polling: External Data (F&G, ETF flow, stablecoin flow, liq clusters)
    # ─────────────────────────────────────────────────────────────────────

    async def _poll_external_data(self) -> None:
        interval = int(self._dcfg.get("external_data_poll_interval_seconds", 300))
        while self._running:
            try:
                await asyncio.gather(
                    self._fetch_fear_greed(),
                    self._fetch_liquidation_clusters(),
                    self._fetch_etf_stablecoin_flow(),
                    return_exceptions=True,
                )
                self._api_health["external"] = True
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("External data poll error: %s", exc)
                self._api_health["external"] = False
            await asyncio.sleep(interval)

    async def _fetch_fear_greed(self) -> None:
        url = self._ext_cfg.get("fear_greed_url", "https://api.alternative.me/fng/")
        try:
            async with self._session.get(url, params={"limit": 1}) as resp:
                resp.raise_for_status()
                data = await resp.json()
            fng = int(data["data"][0]["value"])
            for sym in self._symbols:
                self._market_data[sym].fng_index = fng
            logger.debug("Fear & Greed Index: %d", fng)
        except Exception as exc:
            logger.debug("F&G fetch error: %s", exc)

    async def _fetch_liquidation_clusters(self) -> None:
        """Fetch on-chain liquidation heatmap data from Coinglass (or simulate)."""
        try:
            # Primary symbols only (BTC/ETH via Coinglass public endpoint)
            base_url = self._ext_cfg.get(
                "coinglass_liquidation_url",
                "https://open-api.coinglass.com/public/v2/liquidation_history",
            )
            for sym in ["BTCUSDT", "ETHUSDT"]:
                if sym not in self._market_data:
                    continue
                await self._rate_limiter.acquire(weight=1)
                try:
                    async with self._session.get(
                        base_url,
                        params={"symbol": sym.replace("USDT", ""), "timeType": "1", "limit": "5"},
                        headers={"coinglassSecret": ""},  # public endpoint — no key needed
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            self._parse_liq_clusters(sym, data)
                except Exception:
                    pass
                # Fallback: estimate clusters from funding rate + OI
                self._estimate_liq_clusters(sym)
        except Exception as exc:
            logger.debug("Liq cluster fetch error: %s", exc)
            for sym in self._symbols:
                self._estimate_liq_clusters(sym)

    def _parse_liq_clusters(self, symbol: str, data: dict) -> None:
        try:
            md = self._market_data[symbol]
            items = data.get("data", [])
            if not items:
                return
            long_liq  = [(float(i.get("longLiquidationUSD", 0)), float(i.get("price", 0))) for i in items]
            short_liq = [(float(i.get("shortLiquidationUSD", 0)), float(i.get("price", 0))) for i in items]
            long_liq.sort(reverse=True)
            short_liq.sort(reverse=True)
            if long_liq and long_liq[0][1] > 0:
                md.liquidation_clusters["long_cluster_price"] = long_liq[0][1]
                md.liquidation_clusters["long_cluster_size"]  = long_liq[0][0]
            if short_liq and short_liq[0][1] > 0:
                md.liquidation_clusters["short_cluster_price"] = short_liq[0][1]
                md.liquidation_clusters["short_cluster_size"]  = short_liq[0][0]
        except Exception as exc:
            logger.debug("_parse_liq_clusters(%s): %s", symbol, exc)

    def _estimate_liq_clusters(self, symbol: str) -> None:
        """Proxy liq clusters from OI and price when API unavailable."""
        try:
            md = self._market_data.get(symbol)
            if md is None or md.price <= 0 or md.open_interest <= 0:
                return
            p   = md.price
            oi  = md.open_interest * p   # notional USD
            fr  = md.funding_rate
            atr_val = md.atr if md.atr > 0 else p * 0.005
            # Positive funding → lots of longs → long liq cluster below
            # Negative funding → lots of shorts → short liq cluster above
            if fr >= 0:
                md.liquidation_clusters["long_cluster_price"] = p - atr_val * 2.5
                md.liquidation_clusters["long_cluster_size"]  = oi * abs(fr) * 5_000
                md.liquidation_clusters["short_cluster_price"] = p + atr_val * 3.0
                md.liquidation_clusters["short_cluster_size"]  = oi * 0.01
            else:
                md.liquidation_clusters["short_cluster_price"] = p + atr_val * 2.5
                md.liquidation_clusters["short_cluster_size"]  = oi * abs(fr) * 5_000
                md.liquidation_clusters["long_cluster_price"]  = p - atr_val * 3.0
                md.liquidation_clusters["long_cluster_size"]   = oi * 0.01
        except Exception as exc:
            logger.debug("_estimate_liq_clusters(%s): %s", symbol, exc)

    async def _fetch_etf_stablecoin_flow(self) -> None:
        """Fetch or simulate ETF/stablecoin flow data."""
        try:
            # In production: replace with Glassnode / CryptoQuant API calls
            # Simulation: Use BTC funding rate as a proxy for ETF flow direction
            btc_md = self._market_data.get("BTCUSDT")
            if btc_md:
                fr = btc_md.funding_rate
                # Positive funding → longs dominant → ETF inflow proxy
                etf_proxy    = fr * 1_000_000_000     # scale to USD
                stable_proxy = fr * 500_000_000
                for sym in self._symbols:
                    self._market_data[sym].etf_net_flow       = etf_proxy
                    self._market_data[sym].stablecoin_net_flow = stable_proxy
        except Exception as exc:
            logger.debug("_fetch_etf_stablecoin_flow: %s", exc)

    # ─────────────────────────────────────────────────────────────────────
    # DB flush loop
    # ─────────────────────────────────────────────────────────────────────

    async def _flush_db_loop(self) -> None:
        flush_interval = int(self._cfg.get("bot", {}).get("db_flush_interval_seconds", 60))
        while self._running:
            await asyncio.sleep(flush_interval)
            try:
                tasks = [
                    self._db.insert_market_data(md)
                    for md in self._market_data.values()
                    if md.price > 0
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
                logger.debug("DB flush: %d market_data records written.", len(tasks))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("DB flush error: %s", exc)
