"""
KAVACH-07 — Data Engine
WebSocket streams + REST fallback + SQLite persistence.
True CVD from @aggTrade. Automatic reconnection with exponential backoff.
"""
from __future__ import annotations

import asyncio
import json
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, Deque, Tuple

import aiohttp
import ujson
import websockets
import websockets.exceptions

from config import Config
from database import Database
from models import DataSnapshot, RegimeSignal
from utils import (
    RateLimiter, RollingBuffer, calc_atr, calc_slope, calc_volume_profile,
    find_swing_high, find_swing_low, calc_volume_ratio, get_logger,
)

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

# Max candles kept per symbol per timeframe
_CANDLE_MAXLEN = 500
# CVD history length (for z-score / slope)
_CVD_HIST_LEN = 300
# OI history
_OI_HIST_LEN = 300
# REST poll intervals (seconds)
_OI_POLL_INTERVAL = 60
_FUNDING_POLL_INTERVAL = 300
_FNG_POLL_INTERVAL = 600

# Fear & Greed API
_FNG_URL = "https://api.alternative.me/fng/?limit=1&format=json"


# ─────────────────────────────────────────────────────────────
# DataEngine
# ─────────────────────────────────────────────────────────────

class DataEngine:
    """
    Central data hub. Manages all WebSocket streams and REST polling.
    Thread-safe (asyncio coroutines only).
    """

    def __init__(self, config: Config, db: Database):
        self._cfg = config
        self._db = db

        # Per-symbol candle buffers: symbol -> interval -> deque[dict]
        self._candles: Dict[str, Dict[str, Deque]] = {
            sym: {tf: deque(maxlen=_CANDLE_MAXLEN) for tf in config.TIMEFRAMES}
            for sym in config.BASE_PAIRS
        }

        # Orderbook: symbol -> (bids, asks, timestamp)
        self._ob: Dict[str, Tuple[list, list, float]] = {}

        # CVD tracking
        self._cvd: Dict[str, float] = defaultdict(float)
        self._cvd_history: Dict[str, RollingBuffer] = {
            sym: RollingBuffer(_CVD_HIST_LEN) for sym in config.BASE_PAIRS
        }
        self._delta_1m: Dict[str, float] = defaultdict(float)
        self._delta_window: Dict[str, float] = defaultdict(float)  # reset every 60s

        # Funding & OI
        self._funding: Dict[str, dict] = {}
        self._funding_history: Dict[str, RollingBuffer] = {
            sym: RollingBuffer(360) for sym in config.BASE_PAIRS
        }
        self._oi: Dict[str, float] = {}
        self._oi_history: Dict[str, RollingBuffer] = {
            sym: RollingBuffer(_OI_HIST_LEN) for sym in config.BASE_PAIRS
        }

        # Mark / index prices
        self._mark_price: Dict[str, float] = {}
        self._index_price: Dict[str, float] = {}

        # Bybit prices (for exchange arb)
        self._bybit_prices: Dict[str, float] = {}

        # Fear & Greed
        self._fear_greed: int = 50

        # Regime signal (updated by regime_filter strategy)
        self._regime: Optional[RegimeSignal] = None

        # Health
        self.last_ws_msg: float = time.time()
        self.ws_reconnects: int = 0

        # Rate limiter: 1200 requests/min
        self._rl = RateLimiter(rate=1000, per_seconds=60.0)

        # HTTP session (shared)
        self._session: Optional[aiohttp.ClientSession] = None

        # Shutdown flag
        self._shutdown = False

        # Initialisation gate — set when first data has arrived
        self._ready = asyncio.Event()
        self._tasks: List[asyncio.Task] = []

    # ─── Lifecycle ───────────────────────────────────────────

    async def start(self) -> None:
        """Start all data streams and wait for initial data."""
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=32),
            json_serialize=ujson.dumps,
            timeout=aiohttp.ClientTimeout(total=10),
        )

        # Load historical candles (REST)
        await self._bootstrap_candles()

        # Load saved OI + funding history from DB
        await self._load_history_from_db()

        self._ready.set()  # Mark as ready before WS (WS enriches but not required)

        # Start WebSocket tasks
        self._tasks = [
            asyncio.create_task(self._ws_klines(), name="ws_klines"),
            asyncio.create_task(self._ws_trades_depth(), name="ws_trades_depth"),
            asyncio.create_task(self._ws_mark_prices(), name="ws_mark_prices"),
            asyncio.create_task(self._rest_oi_loop(), name="rest_oi"),
            asyncio.create_task(self._rest_funding_loop(), name="rest_funding"),
            asyncio.create_task(self._rest_fng_loop(), name="rest_fng"),
            asyncio.create_task(self._bybit_price_loop(), name="bybit_prices"),
            asyncio.create_task(self._delta_reset_loop(), name="delta_reset"),
        ]
        logger.info("DataEngine started — all streams launching")

    async def stop(self) -> None:
        self._shutdown = True
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._session:
            await self._session.close()
        logger.info("DataEngine stopped")

    async def wait_ready(self) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=60.0)

    # ─── Snapshot ────────────────────────────────────────────

    def get_snapshot(self, symbol: str) -> Optional[DataSnapshot]:
        """Build a complete DataSnapshot for a symbol. Returns None if insufficient data."""
        if symbol not in self._candles:
            return None

        c1m = list(self._candles[symbol]["1m"])
        c5m = list(self._candles[symbol]["5m"])
        c15m = list(self._candles[symbol]["15m"])
        c1h = list(self._candles[symbol]["1h"])

        if len(c5m) < 20:  # Need minimum history
            return None

        # Orderbook
        ob = self._ob.get(symbol, ([], [], 0.0))
        bids, asks = ob[0], ob[1]

        # Mark price
        mark = self._mark_price.get(symbol, 0.0)
        index = self._index_price.get(symbol, mark)

        # Funding
        fund_data = self._funding.get(symbol, {})
        fund_rate = fund_data.get("funding_rate", 0.0)
        fund_hist = self._funding_history[symbol].to_list()

        # OI
        oi = self._oi.get(symbol, 0.0)
        oi_hist = self._oi_history[symbol].to_list()

        # CVD
        cvd = self._cvd[symbol]
        cvd_hist = self._cvd_history[symbol].to_list()
        cvd_z = self._cvd_history[symbol].z_score(cvd)
        cvd_slope_5m = calc_slope(cvd_hist[-60:]) if len(cvd_hist) >= 10 else 0.0
        cvd_slope_15m = calc_slope(cvd_hist[-180:]) if len(cvd_hist) >= 30 else 0.0

        # ATR
        atr_1m = calc_atr(c1m, 14) if len(c1m) >= 15 else 0.0
        atr_5m = calc_atr(c5m, 14) if len(c5m) >= 15 else 0.0
        atr_1h = calc_atr(c1h, 14) if len(c1h) >= 15 else 0.0

        # Spread
        spread_pct = 0.0
        if bids and asks:
            best_bid = bids[0][0] if bids else 0.0
            best_ask = asks[0][0] if asks else 0.0
            if best_bid > 0:
                spread_pct = (best_ask - best_bid) / best_bid

        # OB imbalance
        ob_imbalance = 1.0
        if bids and asks:
            bvol = sum(b[1] for b in bids[:10])
            avol = sum(a[1] for a in asks[:10])
            ob_imbalance = bvol / avol if avol > 1e-10 else 1.0

        # Volume ratio
        vol_ratio = calc_volume_ratio(c1m, 20) if len(c1m) >= 21 else 1.0

        # Funding percentile
        fund_pct = self._funding_history[symbol].percentile(fund_rate) if fund_hist else 50.0

        # OI changes
        oi_change_1h, oi_change_4h = self._calc_oi_change(symbol)

        # Volume profile (session = 1h candles)
        poc, vah, val, lvns, hvns = (0.0, 0.0, 0.0, [], [])
        if len(c1h) >= 5:
            poc, vah, val, lvns, hvns = calc_volume_profile(c1h[-24:])

        # Swing levels
        swing_high_5m = find_swing_high(c5m, 20) if len(c5m) >= 5 else 0.0
        swing_low_5m = find_swing_low(c5m, 20) if len(c5m) >= 5 else 0.0
        swing_high_1h = find_swing_high(c1h, 20) if len(c1h) >= 5 else 0.0
        swing_low_1h = find_swing_low(c1h, 20) if len(c1h) >= 5 else 0.0

        # Delta direction
        delta = self._delta_1m.get(symbol, 0.0)
        delta_dir = 1 if delta > 0 else (-1 if delta < 0 else 0)

        return DataSnapshot(
            symbol=symbol,
            timestamp=datetime.now(timezone.utc),
            candles_1m=c1m,
            candles_5m=c5m,
            candles_15m=c15m,
            candles_1h=c1h,
            bids=bids,
            asks=asks,
            cvd=cvd,
            cvd_z_score=cvd_z,
            cvd_slope_5m=cvd_slope_5m,
            cvd_slope_15m=cvd_slope_15m,
            delta_1m=delta,
            delta_direction=delta_dir,
            mark_price=mark,
            index_price=index,
            funding_rate=fund_rate,
            funding_history=fund_hist,
            open_interest=oi,
            oi_history=oi_hist,
            oi_change_1h=oi_change_1h,
            oi_change_4h=oi_change_4h,
            atr_1m=atr_1m,
            atr_5m=atr_5m,
            atr_1h=atr_1h,
            spread_pct=spread_pct,
            ob_imbalance=ob_imbalance,
            volume_ratio=vol_ratio,
            funding_percentile=fund_pct,
            poc=poc,
            vah=vah,
            val=val,
            lvns=lvns,
            hvns=hvns,
            swing_high_5m=swing_high_5m,
            swing_low_5m=swing_low_5m,
            swing_high_1h=swing_high_1h,
            swing_low_1h=swing_low_1h,
            bybit_price=self._bybit_prices.get(symbol, 0.0),
            fear_greed_index=self._fear_greed,
        )

    def get_current_price(self, symbol: str) -> float:
        """Best approximation of current price (for heartbeat checks)."""
        mark = self._mark_price.get(symbol, 0.0)
        if mark > 0:
            return mark
        ob = self._ob.get(symbol)
        if ob and ob[0] and ob[1]:
            return (ob[0][0][0] + ob[1][0][0]) / 2
        # Fallback to last close
        if symbol in self._candles and self._candles[symbol]["1m"]:
            return float(list(self._candles[symbol]["1m"])[-1]["close"])
        return 0.0

    def set_regime(self, regime: RegimeSignal) -> None:
        self._regime = regime

    def get_regime(self) -> Optional[RegimeSignal]:
        return self._regime

    # ─── OI change helper ────────────────────────────────────

    def _calc_oi_change(self, symbol: str) -> Tuple[float, float]:
        hist = self._oi_history[symbol].to_list()
        if not hist or len(hist) < 2:
            return 0.0, 0.0
        current = hist[-1]
        if current < 1e-10:
            return 0.0, 0.0

        # 1h change: ~60 samples at 1/min
        idx_1h = max(0, len(hist) - 60)
        past_1h = hist[idx_1h]
        change_1h = (current - past_1h) / past_1h if past_1h > 1e-10 else 0.0

        # 4h change: ~240 samples
        idx_4h = max(0, len(hist) - 240)
        past_4h = hist[idx_4h]
        change_4h = (current - past_4h) / past_4h if past_4h > 1e-10 else 0.0

        return change_1h, change_4h

    # ─── Bootstrap (REST historical candles) ─────────────────

    async def _bootstrap_candles(self) -> None:
        logger.info("Bootstrapping historical candles...")
        symbols = self._cfg.BASE_PAIRS
        timeframes = self._cfg.TIMEFRAMES

        # Process in batches to avoid rate limit burst
        batch_size = 5
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            tasks = [
                self._fetch_candles_rest(sym, tf)
                for sym in batch
                for tf in timeframes
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0.5)

        logger.info("Historical candles loaded")

    async def _load_history_from_db(self) -> None:
        """Load OI/funding history from SQLite."""
        for sym in self._cfg.BASE_PAIRS:
            # Load funding history
            rates = await self._db.get_funding_history(sym, limit=360)
            for r in rates:
                self._funding_history[sym].append(r)

            # Load OI history
            oi_rows = await self._db.get_oi_history(sym, limit=300)
            for oi, _ in oi_rows:
                self._oi_history[sym].append(oi)

    async def _fetch_candles_rest(self, symbol: str, interval: str) -> None:
        try:
            await self._rl.acquire(2)
            url = f"{self._cfg.BINANCE_REST_BASE}/fapi/v1/klines"
            params = {"symbol": symbol, "interval": interval, "limit": 300}
            async with self._session.get(url, params=params) as resp:  # type: ignore
                if resp.status != 200:
                    return
                data = await resp.json(loads=ujson.loads)

            for k in data:
                candle = {
                    "symbol": symbol, "interval": interval,
                    "open_time": int(k[0]), "open": float(k[1]),
                    "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "volume": float(k[5]),
                    "quote_volume": float(k[7]), "num_trades": int(k[8]),
                }
                self._candles[symbol][interval].append(candle)

        except Exception as e:
            logger.warning(f"Bootstrap candles {symbol}/{interval}: {e}")

    # ─── WebSocket: Klines ───────────────────────────────────

    async def _ws_klines(self) -> None:
        streams = [
            f"{sym.lower()}@kline_{tf}"
            for sym in self._cfg.BASE_PAIRS
            for tf in self._cfg.TIMEFRAMES
        ]
        await self._managed_ws(streams, self._handle_kline_msg, "klines")

    async def _handle_kline_msg(self, msg: dict) -> None:
        k = msg.get("k", {})
        symbol = k.get("s", "")
        interval = k.get("i", "")
        if not symbol or symbol not in self._candles:
            return

        candle = {
            "symbol": symbol, "interval": interval,
            "open_time": int(k["t"]), "open": float(k["o"]),
            "high": float(k["h"]), "low": float(k["l"]),
            "close": float(k["c"]), "volume": float(k["v"]),
            "quote_volume": float(k.get("q", 0)),
            "num_trades": int(k.get("n", 0)),
            "is_closed": bool(k.get("x", False)),
        }
        buf = self._candles[symbol][interval]

        # Replace last candle if same open_time, else append
        if buf and buf[-1]["open_time"] == candle["open_time"]:
            buf[-1] = candle
        else:
            buf.append(candle)

        # Persist closed candles to DB
        if candle["is_closed"]:
            asyncio.create_task(self._db.upsert_candle(candle))

    # ─── WebSocket: Trades + Depth ───────────────────────────

    async def _ws_trades_depth(self) -> None:
        streams = []
        for sym in self._cfg.BASE_PAIRS:
            s = sym.lower()
            streams.append(f"{s}@aggTrade")
            streams.append(f"{s}@depth20@100ms")
        await self._managed_ws(streams, self._handle_trade_depth_msg, "trades_depth")

    async def _handle_trade_depth_msg(self, msg: dict) -> None:
        stream = msg.get("stream", "")

        if "@aggTrade" in stream:
            await self._handle_agg_trade(msg.get("data", msg))
        elif "@depth20" in stream:
            self._handle_depth(msg.get("data", msg))

    async def _handle_agg_trade(self, data: dict) -> None:
        symbol = data.get("s", "")
        if not symbol or symbol not in self._cvd:
            if symbol:
                self._cvd[symbol] = 0.0
        if not symbol:
            return

        qty = float(data.get("q", 0))
        is_buyer_maker = bool(data.get("m", False))
        # buyer_maker=True → taker is seller → aggressive sell
        delta = -qty if is_buyer_maker else qty

        self._cvd[symbol] += delta
        self._delta_window[symbol] += delta
        self._cvd_history[symbol].append(self._cvd[symbol])

    def _handle_depth(self, data: dict) -> None:
        symbol = data.get("s", "")
        if not symbol:
            return
        bids = [[float(p), float(q)] for p, q in data.get("b", [])]
        asks = [[float(p), float(q)] for p, q in data.get("a", [])]
        self._ob[symbol] = (bids, asks, time.time())

    # ─── WebSocket: Mark Prices ──────────────────────────────

    async def _ws_mark_prices(self) -> None:
        streams = [f"{sym.lower()}@markPrice@1s" for sym in self._cfg.BASE_PAIRS]
        await self._managed_ws(streams, self._handle_mark_price_msg, "mark_prices")

    async def _handle_mark_price_msg(self, msg: dict) -> None:
        data = msg.get("data", msg)
        symbol = data.get("s", "")
        if not symbol:
            return

        self._mark_price[symbol] = float(data.get("p", 0))
        self._index_price[symbol] = float(data.get("i", 0))

        fund_rate = float(data.get("r", 0))
        fund_time = int(data.get("T", 0))

        if symbol in self._funding:
            if self._funding[symbol].get("funding_time") != fund_time:
                # New funding period
                self._funding[symbol] = {
                    "funding_rate": fund_rate,
                    "funding_time": fund_time,
                    "mark_price": self._mark_price[symbol],
                    "index_price": self._index_price[symbol],
                }
                self._funding_history[symbol].append(fund_rate)
                asyncio.create_task(
                    self._db.upsert_funding(
                        symbol, fund_rate, fund_time,
                        self._mark_price[symbol], self._index_price[symbol],
                    )
                )
        else:
            self._funding[symbol] = {
                "funding_rate": fund_rate,
                "funding_time": fund_time,
                "mark_price": self._mark_price[symbol],
                "index_price": self._index_price[symbol],
            }

    # ─── REST: Open Interest ─────────────────────────────────

    async def _rest_oi_loop(self) -> None:
        while not self._shutdown:
            try:
                for sym in self._cfg.BASE_PAIRS:
                    if self._shutdown:
                        break
                    await self._rl.acquire(1)
                    url = f"{self._cfg.BINANCE_REST_BASE}/fapi/v1/openInterest"
                    async with self._session.get(url, params={"symbol": sym}) as resp:  # type: ignore
                        if resp.status == 200:
                            data = await resp.json(loads=ujson.loads)
                            oi = float(data.get("openInterest", 0))
                            oi_val = float(data.get("openInterestValue", oi * self.get_current_price(sym)))
                            self._oi[sym] = oi
                            self._oi_history[sym].append(oi)
                            asyncio.create_task(self._db.insert_oi(sym, oi, oi_val))
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"OI poll error: {e}")
            await asyncio.sleep(_OI_POLL_INTERVAL)

    # ─── REST: Funding History ────────────────────────────────

    async def _rest_funding_loop(self) -> None:
        while not self._shutdown:
            try:
                for sym in self._cfg.BASE_PAIRS:
                    if self._shutdown:
                        break
                    await self._rl.acquire(1)
                    url = f"{self._cfg.BINANCE_REST_BASE}/fapi/v1/fundingRate"
                    params = {"symbol": sym, "limit": 100}
                    async with self._session.get(url, params=params) as resp:  # type: ignore
                        if resp.status == 200:
                            data = await resp.json(loads=ujson.loads)
                            for item in data:
                                rate = float(item.get("fundingRate", 0))
                                self._funding_history[sym].append(rate)
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Funding history poll error: {e}")
            await asyncio.sleep(_FUNDING_POLL_INTERVAL)

    # ─── REST: Fear & Greed ──────────────────────────────────

    async def _rest_fng_loop(self) -> None:
        while not self._shutdown:
            try:
                async with self._session.get(_FNG_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:  # type: ignore
                    if resp.status == 200:
                        data = await resp.json(loads=ujson.loads)
                        items = data.get("data", [])
                        if items:
                            self._fear_greed = int(items[0].get("value", 50))
                            logger.debug(f"Fear & Greed index: {self._fear_greed}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"F&G fetch error: {e}")
            await asyncio.sleep(_FNG_POLL_INTERVAL)

    # ─── Bybit prices (Exchange Arb strategy) ────────────────

    async def _bybit_price_loop(self) -> None:
        while not self._shutdown:
            try:
                for sym in self._cfg.BASE_PAIRS:
                    if self._shutdown:
                        break
                    url = f"{self._cfg.BYBIT_API_URL}/v5/market/tickers"
                    params = {"category": "linear", "symbol": sym}
                    try:
                        async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:  # type: ignore
                            if resp.status == 200:
                                data = await resp.json(loads=ujson.loads)
                                tickers = data.get("result", {}).get("list", [])
                                if tickers:
                                    self._bybit_prices[sym] = float(tickers[0].get("lastPrice", 0))
                    except Exception:
                        pass
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"Bybit price loop error: {e}")
            await asyncio.sleep(10)

    # ─── Delta reset (per-minute delta window) ───────────────

    async def _delta_reset_loop(self) -> None:
        while not self._shutdown:
            await asyncio.sleep(60)
            for sym in self._cfg.BASE_PAIRS:
                self._delta_1m[sym] = self._delta_window[sym]
                self._delta_window[sym] = 0.0

    # ─── Generic WebSocket manager ───────────────────────────

    async def _managed_ws(self, streams: List[str], handler, name: str) -> None:
        delay = self._cfg.WS_RECONNECT_DELAY
        max_delay = self._cfg.MAX_WS_RECONNECT_DELAY
        base = self._cfg.BINANCE_WS_BASE

        while not self._shutdown:
            url = f"{base}/stream?streams=" + "/".join(streams)
            try:
                logger.info(f"WS[{name}] connecting ({len(streams)} streams)...")
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=60,
                    close_timeout=5,
                    max_size=2**23,
                ) as ws:
                    delay = self._cfg.WS_RECONNECT_DELAY  # reset on success
                    logger.info(f"WS[{name}] connected")
                    async for raw in ws:
                        if self._shutdown:
                            break
                        self.last_ws_msg = time.time()
                        try:
                            msg = ujson.loads(raw)
                            await handler(msg)
                        except Exception:
                            logger.debug(f"WS[{name}] msg parse error: {traceback.format_exc()}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._shutdown:
                    break
                self.ws_reconnects += 1
                logger.warning(f"WS[{name}] disconnected: {e} — retry in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)

        logger.info(f"WS[{name}] task ended")
