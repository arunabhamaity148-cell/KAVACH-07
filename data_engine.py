"""
KAVACH-07 — Data Engine
WebSocket + REST dual pipeline for Binance Futures.
Feeds: klines, orderbook, trades, mark price, funding, OI.
Also polls Bybit for exchange arb.
"""
from __future__ import annotations

import asyncio
import json
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from config import Config
from models import Candle, DataSnapshot, FundingData, OpenInterestData
from utils import RollingBuffer, get_logger

logger = get_logger(__name__)

class DataEngine:

    def __init__(self, config: Config):
        self._cfg = config
        self._session: Optional[aiohttp.ClientSession] = None

        # In-memory state (keyed by symbol)
        self._klines: Dict[str, Dict[str, RollingBuffer]] = {}
        self._orderbook: Dict[str, Dict] = {}
        self._trades: Dict[str, List] = {}
        self._mark_price: Dict[str, float] = {}
        self._funding: Dict[str, Dict] = {}
        self._funding_history: Dict[str, RollingBuffer] = {}
        self._oi: Dict[str, Dict] = {}
        self._oi_history: Dict[str, RollingBuffer] = {}
        self._cvd: Dict[str, float] = {}
        self._cvd_history: Dict[str, RollingBuffer] = {}
        self._last_ws_message: float = 0.0
        self._ws_reconnects: int = 0

        self._bybit_price: Dict[str, float] = {}

        # FIX: Multiple WS connections instead of one overloaded connection
        self._ws_tasks: List[asyncio.Task] = []
        self._ws_sessions: List[aiohttp.ClientWebSocketResponse] = []

        self._shutdown = False

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        await self._bootstrap_candles()
        await self._load_history_from_db()

        # FIX: Split pairs into groups for multiple WS connections
        pair_groups = self._split_pairs(self._cfg.BASE_PAIRS, group_size=5)
        for i, group in enumerate(pair_groups):
            task = asyncio.create_task(
                self._ws_main_loop(group, conn_id=i),
                name=f"ws_conn_{i}"
            )
            self._ws_tasks.append(task)

        # Start periodic REST loops
        asyncio.create_task(self._rest_funding_loop(), name="rest_funding")
        asyncio.create_task(self._rest_oi_loop(), name="rest_oi")
        asyncio.create_task(self._rest_candles_loop(), name="rest_candles")
        asyncio.create_task(self._bybit_price_loop(), name="bybit_price")

        logger.info(f"DataEngine started ({len(self._cfg.BASE_PAIRS)} pairs, {len(pair_groups)} WS connections)")

    async def stop(self) -> None:
        self._shutdown = True
        for task in self._ws_tasks:
            task.cancel()
        await asyncio.gather(*self._ws_tasks, return_exceptions=True)
        if self._session:
            await self._session.close()
        logger.info("DataEngine stopped")

    # FIX: Split pairs into groups for multiple WS connections
    def _split_pairs(self, pairs: List[str], group_size: int = 5) -> List[List[str]]:
        return [pairs[i:i + group_size] for i in range(0, len(pairs), group_size)]

    def get_snapshot(self, symbol: str) -> DataSnapshot:
        now = datetime.now(timezone.utc)
        snap = DataSnapshot(symbol=symbol, timestamp=now)

        # Klines
        for tf in self._cfg.TIMEFRAMES:
            buf = self._klines.get(symbol, {}).get(tf)
            if buf:
                setattr(snap, f"candles_{tf}", list(buf))

        # Orderbook
        ob = self._orderbook.get(symbol, {})
        snap.bids = ob.get("bids", [])
        snap.asks = ob.get("asks", [])
        snap.spread_pct = ob.get("spread_pct", 1.0)
        snap.ob_imbalance = ob.get("imbalance", 1.0)

        # CVD
        snap.cvd = self._cvd.get(symbol, 0.0)
        cvd_hist = self._cvd_history.get(symbol)
        if cvd_hist and len(cvd_hist) > 10:
            snap.cvd_z_score = self._z_score(snap.cvd, list(cvd_hist))
            snap.cvd_slope_5m = self._linear_slope(list(cvd_hist)[-30:])
            snap.cvd_slope_15m = self._linear_slope(list(cvd_hist)[-90:])

        # Mark price / funding / OI
        snap.mark_price = self._mark_price.get(symbol, 0.0)
        fund = self._funding.get(symbol, {})
        snap.funding_rate = fund.get("funding_rate", 0.0)
        snap.index_price = fund.get("index_price", 0.0)
        snap.funding_history = list(self._funding_history.get(symbol, []))
        snap.funding_percentile = self._percentile(snap.funding_rate, snap.funding_history)

        oi = self._oi.get(symbol, {})
        snap.open_interest = oi.get("open_interest", 0.0)
        snap.oi_history = list(self._oi_history.get(symbol, []))
        if len(snap.oi_history) >= 2:
            snap.oi_change_1h = self._pct_change(snap.oi_history, periods=12)
            snap.oi_change_4h = self._pct_change(snap.oi_history, periods=48)

        # ATRs
        for tf in ["1m", "5m", "1h"]:
            buf = self._klines.get(symbol, {}).get(tf)
            if buf and len(buf) >= 14:
                atr = self._calc_atr(list(buf))
                setattr(snap, f"atr_{tf}", atr)

        # Volume profile
        buf_1h = self._klines.get(symbol, {}).get("1h")
        if buf_1h and len(buf_1h) >= 24:
            vp = self._calc_volume_profile(list(buf_1h))
            snap.poc = vp.get("poc", 0.0)
            snap.vah = vp.get("vah", 0.0)
            snap.val = vp.get("val", 0.0)
            snap.lvns = vp.get("lvns", [])
            snap.hvns = vp.get("hvns", [])

        # Swing levels
        buf_5m = self._klines.get(symbol, {}).get("5m")
        if buf_5m and len(buf_5m) >= 20:
            highs = [c["high"] for c in buf_5m]
            lows = [c["low"] for c in buf_5m]
            snap.swing_high_5m = max(highs[-20:])
            snap.swing_low_5m = min(lows[-20:])

        buf_1h = self._klines.get(symbol, {}).get("1h")
        if buf_1h and len(buf_1h) >= 20:
            highs = [c["high"] for c in buf_1h]
            lows = [c["low"] for c in buf_1h]
            snap.swing_high_1h = max(highs[-20:])
            snap.swing_low_1h = min(lows[-20:])

        # Bybit price
        snap.bybit_price = self._bybit_price.get(symbol, 0.0)

        return snap

    def get_current_price(self, symbol: str) -> float:
        return self._mark_price.get(symbol, 0.0)

    async def _ws_main_loop(self, pairs: List[str], conn_id: int) -> None:
        backoff = 1.0
        max_backoff = 60.0

        while not self._shutdown:
            try:
                ws = await self._connect_ws(pairs)
                self._ws_sessions.append(ws)
                logger.info(f"WS conn {conn_id} connected ({len(pairs)} pairs)")
                backoff = 1.0

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._last_ws_message = time.time()
                        await self._handle_ws_message(json.loads(msg.data))
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(f"WS conn {conn_id} error: {ws.exception()}")
                        break
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        logger.warning(f"WS conn {conn_id} closed")
                        break

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"WS conn {conn_id} error: {e}")

            self._ws_reconnects += 1
            logger.warning(f"WS conn {conn_id} reconnecting in {backoff:.1f}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def _connect_ws(self, pairs: List[str]) -> aiohttp.ClientWebSocketResponse:
        streams = []
        for sym in pairs:
            sym_l = sym.lower()
            for tf in self._cfg.TIMEFRAMES:
                streams.append(f"{sym_l}@kline_{tf}")
            streams.append(f"{sym_l}@depth20@100ms")
            streams.append(f"{sym_l}@aggTrade")
            streams.append(f"{sym_l}@markPrice@1s")

        stream_url = f"{self._cfg.WS_BASE}/stream?streams={'/'.join(streams)}"
        return await self._session.ws_connect(stream_url)

    async def _handle_ws_message(self, payload: Dict) -> None:
        data = payload.get("data", {})
        if not data:
            return

        stream = payload.get("stream", "")

        if "@kline_" in stream:
            await self._handle_kline_msg(data)
        elif "@depth20" in stream:
            await self._handle_depth_msg(data)
        elif "@aggTrade" in stream:
            await self._handle_agg_trade(data)
        elif "@markPrice" in stream:
            await self._handle_mark_price_msg(data)

    async def _handle_kline_msg(self, data: Dict) -> None:
        k = data.get("k", {})
        symbol = k.get("s", "")
        tf = k.get("i", "")
        if not symbol or not tf:
            return

        candle = {
            "open_time": datetime.fromtimestamp(k["t"] / 1000, tz=timezone.utc),
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
            "close_time": datetime.fromtimestamp(k["T"] / 1000, tz=timezone.utc),
            "quote_volume": float(k["q"]),
            "num_trades": int(k["n"]),
            "taker_buy_base": float(k["V"]),
            "is_closed": k.get("x", False),
        }

        if symbol not in self._klines:
            self._klines[symbol] = {}
        if tf not in self._klines[symbol]:
            self._klines[symbol][tf] = RollingBuffer(maxlen=200)

        buf = self._klines[symbol][tf]
        if buf and buf[-1]["open_time"] == candle["open_time"]:
            buf[-1] = candle
        else:
            buf.append(candle)

    async def _handle_depth_msg(self, data: Dict) -> None:
        symbol = data.get("s", "")
        if not symbol:
            return

        bids = [[float(p), float(q)] for p, q in data.get("b", [])[:10]]
        asks = [[float(p), float(q)] for p, q in data.get("a", [])[:10]]

        bvol = sum(b[1] for b in bids)
        avol = sum(a[1] for a in asks)
        ob_imbalance = bvol / avol if avol > 1e-10 else 1.0

        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0
        spread_pct = (best_ask - best_bid) / best_bid if best_bid > 0 else 1.0

        self._orderbook[symbol] = {
            "bids": bids,
            "asks": asks,
            "imbalance": ob_imbalance,
            "spread_pct": spread_pct,
            "timestamp": time.time(),
        }

    async def _handle_agg_trade(self, data: Dict) -> None:
        symbol = data.get("s", "")
        if not symbol:
            return

        # FIX: Only process symbols in BASE_PAIRS
        if symbol not in self._cfg.BASE_PAIRS:
            return

        qty = float(data.get("q", 0))
        price = float(data.get("p", 0))
        is_buyer_maker = data.get("m", False)

        delta = qty if not is_buyer_maker else -qty

        if symbol not in self._cvd:
            self._cvd[symbol] = 0.0
        if symbol not in self._cvd_history:
            self._cvd_history[symbol] = RollingBuffer(maxlen=500)

        self._cvd[symbol] += delta * price
        self._cvd_history[symbol].append(self._cvd[symbol])

    async def _handle_mark_price_msg(self, data: Dict) -> None:
        symbol = data.get("s", "")
        if not symbol:
            return

        mark = float(data.get("p", 0))
        self._mark_price[symbol] = mark

        fund_rate = float(data.get("r", 0))
        fund_time = data.get("T", 0)
        index = float(data.get("i", 0))

        if symbol not in self._funding:
            self._funding[symbol] = {}

        prev = self._funding[symbol].get("funding_time")
        if prev != fund_time:
            # New funding period
            if symbol not in self._funding_history:
                self._funding_history[symbol] = RollingBuffer(maxlen=360)
            self._funding_history[symbol].append(fund_rate)

        self._funding[symbol] = {
            "funding_rate": fund_rate,
            "funding_time": fund_time,
            "mark_price": mark,
            "index_price": index,
            "timestamp": time.time(),
        }

    async def _rest_funding_loop(self) -> None:
        while not self._shutdown:
            try:
                for sym in self._cfg.BASE_PAIRS:
                    url = f"{self._cfg.REST_BASE}/fundingRate?symbol={sym}&limit=1"
                    async with self._session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data:
                                item = data[0]
                                rate = float(item.get("fundingRate", 0))
                                fund_time = int(item.get("fundingTime", 0))
                                # FIX: Deduplicate by funding_time
                                current_time = self._funding.get(sym, {}).get("funding_time")
                                if fund_time != current_time:
                                    if sym not in self._funding_history:
                                        self._funding_history[sym] = RollingBuffer(maxlen=360)
                                    self._funding_history[sym].append(rate)
            except Exception as e:
                logger.error(f"Funding REST error: {e}")

            await asyncio.sleep(28_800)

    async def _rest_oi_loop(self) -> None:
        while not self._shutdown:
            try:
                for sym in self._cfg.BASE_PAIRS:
                    url = f"{self._cfg.REST_BASE}/openInterest?symbol={sym}"
                    async with self._session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            oi = float(data.get("openInterest", 0))
                            oi_val = float(data.get("openInterestValue", oi * self.get_current_price(sym)))
                            if sym not in self._oi_history:
                                self._oi_history[sym] = RollingBuffer(maxlen=200)
                            self._oi_history[sym].append(oi)
                            self._oi[sym] = {
                                "open_interest": oi,
                                "open_interest_value": oi_val,
                                "timestamp": time.time(),
                            }
            except Exception as e:
                logger.error(f"OI REST error: {e}")

            await asyncio.sleep(300)

    async def _rest_candles_loop(self) -> None:
        while not self._shutdown:
            try:
                for sym in self._cfg.BASE_PAIRS:
                    for tf in self._cfg.TIMEFRAMES:
                        url = f"{self._cfg.REST_BASE}/klines?symbol={sym}&interval={tf}&limit=10"
                        async with self._session.get(url) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                for item in data:
                                    candle = Candle(
                                        symbol=sym, interval=tf,
                                        open_time=datetime.fromtimestamp(item[0] / 1000, tz=timezone.utc),
                                        open=float(item[1]), high=float(item[2]),
                                        low=float(item[3]), close=float(item[4]),
                                        volume=float(item[5]),
                                        close_time=datetime.fromtimestamp(item[6] / 1000, tz=timezone.utc),
                                        quote_volume=float(item[7]), num_trades=int(item[8]),
                                        taker_buy_base=float(item[9]),
                                    )
                                    if sym not in self._klines:
                                        self._klines[sym] = {}
                                    if tf not in self._klines[sym]:
                                        self._klines[sym][tf] = RollingBuffer(maxlen=200)
                                    buf = self._klines[sym][tf]
                                    exists = any(c["open_time"] == candle.open_time for c in buf)
                                    if not exists:
                                        buf.append({
                                            "open_time": candle.open_time, "open": candle.open,
                                            "high": candle.high, "low": candle.low,
                                            "close": candle.close, "volume": candle.volume,
                                            "close_time": candle.close_time,
                                            "quote_volume": candle.quote_volume,
                                            "num_trades": candle.num_trades,
                                            "taker_buy_base": candle.taker_buy_base,
                                            "is_closed": True,
                                        })
            except Exception as e:
                logger.error(f"Candles REST error: {e}")

            await asyncio.sleep(30)

    async def _bybit_price_loop(self) -> None:
        # FIX: Add rate limiting
        last_request_time = 0
        min_interval = 0.2

        while not self._shutdown:
            try:
                for sym in self._cfg.BASE_PAIRS:
                    # Rate limit
                    now = time.time()
                    elapsed = now - last_request_time
                    if elapsed < min_interval:
                        await asyncio.sleep(min_interval - elapsed)
                    last_request_time = time.time()

                    url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}"
                    async with self._session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            result = data.get("result", {}).get("list", [])
                            if result:
                                price = float(result[0].get("lastPrice", 0))
                                self._bybit_price[sym] = price
            except Exception as e:
                logger.error(f"Bybit price error: {e}")

            await asyncio.sleep(5)

    async def _bootstrap_candles(self) -> None:
        for sym in self._cfg.BASE_PAIRS:
            for tf in self._cfg.TIMEFRAMES:
                try:
                    url = f"{self._cfg.REST_BASE}/klines?symbol={sym}&interval={tf}&limit=200"
                    async with self._session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if sym not in self._klines:
                                self._klines[sym] = {}
                            self._klines[sym][tf] = RollingBuffer(maxlen=200)
                            for item in data:
                                self._klines[sym][tf].append({
                                    "open_time": datetime.fromtimestamp(item[0] / 1000, tz=timezone.utc),
                                    "open": float(item[1]), "high": float(item[2]),
                                    "low": float(item[3]), "close": float(item[4]),
                                    "volume": float(item[5]),
                                    "close_time": datetime.fromtimestamp(item[6] / 1000, tz=timezone.utc),
                                    "quote_volume": float(item[7]),
                                    "num_trades": int(item[8]),
                                    "taker_buy_base": float(item[9]),
                                    "is_closed": True,
                                })
                except Exception as e:
                    logger.error(f"Bootstrap error {sym} {tf}: {e}")

    async def _load_history_from_db(self) -> None:
        pass

    @staticmethod
    def _calc_atr(candles: List[Dict], period: int = 14) -> float:
        if len(candles) < period + 1:
            return 0.0
        trs = []
        for i in range(1, len(candles)):
            high = candles[i]["high"]
            low = candles[i]["low"]
            prev_close = candles[i - 1]["close"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        if len(trs) < period:
            return 0.0
        return sum(trs[-period:]) / period

    @staticmethod
    def _calc_volume_profile(candles: List[Dict], bins: int = 50) -> Dict:
        if not candles:
            return {}
        lows = [c["low"] for c in candles]
        highs = [c["high"] for c in candles]
        min_p, max_p = min(lows), max(highs)
        if max_p <= min_p:
            return {}
        vol_by_price = {}
        bin_size = (max_p - min_p) / bins
        for c in candles:
            mid = (c["low"] + c["high"]) / 2
            bin_idx = int((mid - min_p) / bin_size)
            vol_by_price[bin_idx] = vol_by_price.get(bin_idx, 0) + c["volume"]
        if not vol_by_price:
            return {}
        poc_idx = max(vol_by_price, key=vol_by_price.get)
        poc = min_p + (poc_idx + 0.5) * bin_size
        total_vol = sum(vol_by_price.values())
        cumsum = 0
        vah = val = poc
        for idx in sorted(vol_by_price):
            cumsum += vol_by_price[idx]
            if cumsum >= total_vol * 0.7:
                vah = min_p + (idx + 0.5) * bin_size
                break
        cumsum = 0
        for idx in sorted(vol_by_price, reverse=True):
            cumsum += vol_by_price[idx]
            if cumsum >= total_vol * 0.7:
                val = min_p + (idx + 0.5) * bin_size
                break
        lvns = []
        hvns = []
        avg_vol = total_vol / len(vol_by_price)
        for idx, vol in vol_by_price.items():
            price = min_p + (idx + 0.5) * bin_size
            if vol < avg_vol * 0.3:
                lvns.append(price)
            elif vol > avg_vol * 2:
                hvns.append(price)
        return {"poc": poc, "vah": vah, "val": val, "lvns": lvns, "hvns": hvns}

    @staticmethod
    def _z_score(value: float, history: List[float]) -> float:
        if len(history) < 10:
            return 0.0
        mean = sum(history) / len(history)
        variance = sum((x - mean) ** 2 for x in history) / len(history)
        std = variance ** 0.5
        return (value - mean) / std if std > 1e-10 else 0.0

    @staticmethod
    def _linear_slope(values: List[float]) -> float:
        if len(values) < 2:
            return 0.0
        n = len(values)
        x_mean = (n - 1) / 2
        y_mean = sum(values) / n
        numerator = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
        denominator = sum((i - x_mean) ** 2 for i in range(n))
        return numerator / denominator if denominator > 1e-10 else 0.0

    @staticmethod
    def _percentile(value: float, history: List[float]) -> float:
        if not history:
            return 50.0
        sorted_hist = sorted(history)
        count = sum(1 for h in sorted_hist if h <= value)
        return (count / len(sorted_hist)) * 100

    @staticmethod
    def _pct_change(history: List[float], periods: int) -> float:
        if len(history) < periods + 1:
            return 0.0
        old = history[-periods - 1]
        new = history[-1]
        return (new - old) / old if old > 1e-10 else 0.0
