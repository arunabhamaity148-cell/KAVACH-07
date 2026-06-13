"""
KAVACH-07 Strategy: Liquidation Cascade
Identify the start of a liquidation cascade and RIDE it (trend-following).
Contrast with LiquidationFade which fades the move.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Dict

from .base import Signal, StrategyBase
from ..core.indicators import klines_to_ohlcv, sl_long, sl_short, tp_long, tp_short

logger = logging.getLogger(__name__)


class LiquidationCascade(StrategyBase):
    def __init__(self, config: Dict[str, Any], symbol: str) -> None:
        super().__init__(config, symbol)
        # Rolling window of liquidation events (timestamp, size_usd, side)
        self._liq_window: deque = deque(maxlen=100)
        self._last_signal_ts: float = 0.0

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if md is None or md.price <= 0:
            return self._neutral("No market data")

        cascade_thresh = float(self._cfg.get("cascade_threshold_usd", 2_000_000))
        window_sec     = float(self._cfg.get("cascade_window_seconds", 60))
        sl_pct         = float(self._cfg.get("sl_percent", 1.0))
        tp_pct         = float(self._cfg.get("tp_percent", 2.5))

        # Ingest new liquidation event if present
        liq_event = data_context.get(f"{self.symbol}_liq_event")
        if liq_event is not None:
            liq_size = float(liq_event.get("size_usd", 0.0))
            liq_side = str(liq_event.get("side", ""))
            liq_ts   = float(liq_event.get("timestamp", time.time()))
            if liq_size > 0 and liq_side in ("LONG", "SHORT"):
                self._liq_window.append((liq_ts, liq_size, liq_side))

        price = md.price

        try:
            now = time.time()
            # Sum liquidations within the cascade window, by side
            long_liq_usd  = 0.0
            short_liq_usd = 0.0
            for ts, sz, side in self._liq_window:
                if now - ts <= window_sec:
                    if side == "LONG":
                        long_liq_usd  += sz
                    elif side == "SHORT":
                        short_liq_usd += sz

            # Debounce: don't signal more than once per 5 minutes per symbol
            if now - self._last_signal_ts < 300:
                return self._neutral("Cascade signal cooldown (5m)")

            if long_liq_usd >= cascade_thresh:
                # Large LONG cascade → price dropping fast → RIDE THE DROP → SHORT
                magnitude  = long_liq_usd / cascade_thresh
                klines     = md.kline_data
                momentum_bonus = 0.0
                if len(klines) >= 3:
                    _, _, _, c, _ = klines_to_ohlcv(klines)
                    recent_drop = (c[-3] - c[-1]) / (c[-3] + 1e-10) * 100.0
                    momentum_bonus = min(10.0, recent_drop * 2.0)

                confidence = min(92.0, 55.0 + magnitude * 8.0 + momentum_bonus)
                self._last_signal_ts = now
                rationale = (
                    f"LONG CASCADE: ${long_liq_usd/1e6:.1f}M longs liquidated in "
                    f"{window_sec:.0f}s → Cascade ongoing → SHORT"
                )
                return self._create_signal(
                    self.symbol, "SHORT", confidence, price,
                    sl_short(price, sl_pct), tp_short(price, tp_pct), rationale
                )

            elif short_liq_usd >= cascade_thresh:
                # Large SHORT cascade → price rising fast → RIDE THE PUMP → LONG
                magnitude  = short_liq_usd / cascade_thresh
                klines     = md.kline_data
                momentum_bonus = 0.0
                if len(klines) >= 3:
                    _, _, _, c, _ = klines_to_ohlcv(klines)
                    recent_pump = (c[-1] - c[-3]) / (c[-3] + 1e-10) * 100.0
                    momentum_bonus = min(10.0, recent_pump * 2.0)

                confidence = min(92.0, 55.0 + magnitude * 8.0 + momentum_bonus)
                self._last_signal_ts = now
                rationale = (
                    f"SHORT CASCADE: ${short_liq_usd/1e6:.1f}M shorts liquidated in "
                    f"{window_sec:.0f}s → Cascade ongoing → LONG"
                )
                return self._create_signal(
                    self.symbol, "LONG", confidence, price,
                    sl_long(price, sl_pct), tp_long(price, tp_pct), rationale
                )

            return self._neutral(
                f"No cascade: LONG liq ${long_liq_usd/1e3:.0f}K | "
                f"SHORT liq ${short_liq_usd/1e3:.0f}K in {window_sec:.0f}s "
                f"(threshold ${cascade_thresh/1e6:.1f}M)"
            )

        except Exception as exc:
            logger.error("LiquidationCascade[%s] error: %s", self.symbol, exc, exc_info=True)
            return self._neutral(f"Error: {exc}")
