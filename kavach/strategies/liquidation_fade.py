"""
KAVACH-07 — Liquidation Fade Strategy
Signals contrarian trades to 'fade' the immediate price impact of large single
liquidation events, anticipating a mean-reversion rebound.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.liquidation_fade")

class LiquidationFade(StrategyBase):
    """
    Logic:
    1. Monitor real-time liquidation events (from !forceOrder stream).
    2. Threshold: Single event must be >= min_liquidation_usd (default $500k).
    3. Window: Event must have occurred within the last fade_window_seconds.
    4. Direction (Contrarian):
       - LONG Liquidation (Aggressive Selling) -> Anticipate Bounce -> LONG.
       - SHORT Liquidation (Aggressive Buying) -> Anticipate Pullback -> SHORT.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        min_liq_usd = float(self._cfg.get("min_liquidation_usd", 500000.0))
        window_sec = int(self._cfg.get("fade_window_seconds", 120))
        sl_pct = float(self._cfg.get("sl_percent", 0.5)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 1.0)) / 100.0

        # Retrieve the latest liquidation event for this symbol
        # Expected structure: {"timestamp": float, "side": "BUY"|"SELL", "usd_size": float}
        liq_event: Optional[Dict[str, Any]] = data_context.get(f"{self.symbol}_liq_event")

        if not liq_event:
            return self._neutral("No recent liquidation events tracked")

        try:
            now = time.time()
            event_ts = liq_event["timestamp"]
            event_size = liq_event["usd_size"]
            # side: 'SELL' means a LONG position was liquidated (selling to close)
            # side: 'BUY' means a SHORT position was liquidated (buying to close)
            event_side = liq_event["side"]

            # 1. Recency Check
            age = now - event_ts
            if age > window_sec:
                return self._neutral(f"Liquidation event too old ({age:.1f}s)")

            # 2. Magnitude Check
            if event_size < min_liq_usd:
                return self._neutral(f"Liquidation size (${event_size/1e3:.1f}k) below threshold")

            # 3. Determine Direction (Contrarian Fade)
            if event_side == "SELL":
                # Longs liquidated -> Price pushed down -> Fade LONG
                side = "LONG"
                rationale = (
                    f"Liquidation Fade: Large LONG liquidation detected (${event_size/1e3:.1f}k). "
                    f"Price spike down anticipated to reverse. Fading with LONG."
                )
            elif event_side == "BUY":
                # Shorts liquidated -> Price pushed up -> Fade SHORT
                side = "SHORT"
                rationale = (
                    f"Liquidation Fade: Large SHORT liquidation detected (${event_size/1e3:.1f}k). "
                    f"Price spike up anticipated to reverse. Fading with SHORT."
                )
            else:
                return self._neutral(f"Unknown liquidation side: {event_side}")

            # 4. Confidence Calculation
            # Base 65% + bonus for size (up to 20%) and freshness (up to 10%)
            size_bonus = min(20.0, (event_size / min_liq_usd - 1.0) * 5.0)
            freshness_bonus = max(0.0, (1.0 - (age / window_sec)) * 10.0)
            conf = 65.0 + size_bonus + freshness_bonus
            conf = min(95.0, conf)

            entry = md.price
            if side == "LONG":
                sl = entry * (1.0 - sl_pct)
                tp = entry * (1.0 + tp_pct)
            else:
                sl = entry * (1.0 + sl_pct)
                tp = entry * (1.0 - tp_pct)

            return self._create_signal(
                side=side,
                confidence=conf,
                entry=entry,
                stop_loss=sl,
                take_profit=tp,
                rationale=rationale,
                extra_metadata={
                    "liq_size_usd": round(event_size, 2),
                    "liq_side": event_side,
                    "event_age_sec": round(age, 1)
                }
            )

        except Exception as e:
            logger.error("LiquidationFade error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")