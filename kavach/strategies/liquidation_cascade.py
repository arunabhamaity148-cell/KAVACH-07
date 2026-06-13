"""
KAVACH-07 — Liquidation Cascade Strategy
Identifies the onset of a liquidation cascade (rapid succession of large liquidations)
and signals a momentum-following trade to ride the volatility.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.liquidation_cascade")

class LiquidationCascade(StrategyBase):
    """
    Logic:
    1. Monitor real-time liquidation events (from !forceOrder@arr stream).
    2. Threshold: Cumulative liquidations in a window must be >= min_liquidation_usd (default $2M).
    3. Window: Cascade window is typically short (default 60s).
    4. Direction (Momentum Following):
       - SELL Side Liquidations (Longs getting forced out) -> Price is crashing -> SHORT.
       - BUY Side Liquidations (Shorts getting forced out) -> Price is mooning -> LONG.
    
    Difference from LiquidationFade: Fade looks for a single snap-back; Cascade rides the trend.
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        # Config parameters
        min_liq_usd = float(self._cfg.get("min_liquidation_usd", 2000000.0))
        window_sec = int(self._cfg.get("cascade_window_seconds", 60))
        sl_pct = float(self._cfg.get("sl_percent", 1.0)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 3.0)) / 100.0

        # Retrieve liquidation history for this symbol
        # Expected from DataEngine: a list/deque of {"timestamp": float, "side": "BUY"|"SELL", "usd_size": float}
        liq_history: Optional[List[Dict[str, Any]]] = data_context.get(f"{self.symbol}_liq_history")

        if not liq_history:
            return self._neutral("No liquidation history tracked")

        try:
            now = time.time()
            
            # 1. Filter events within the 60s window
            recent_events = [
                e for e in liq_history 
                if (now - e["timestamp"]) <= window_sec
            ]

            if not recent_events:
                return self._neutral(f"No liquidation events in the last {window_sec}s")

            # 2. Aggregate volume by side
            # side: 'SELL' = Long Liquidations (Price Down)
            # side: 'BUY' = Short Liquidations (Price Up)
            long_liq_vol = sum(e["usd_size"] for e in recent_events if e["side"] == "SELL")
            short_liq_vol = sum(e["usd_size"] for e in recent_events if e["side"] == "BUY")

            # 3. Check for Cascade Threshold
            is_long_cascade = long_liq_vol >= min_liq_usd
            is_short_cascade = short_liq_vol >= min_liq_usd

            if not (is_long_cascade or is_short_cascade):
                return self._neutral(
                    f"Liq volume (L: ${long_liq_vol/1e6:.1f}M, S: ${short_liq_vol/1e6:.1f}M) below cascade threshold"
                )

            # 4. Determine Direction (Momentum Following)
            # If longs are being wiped, join the sellers.
            if is_long_cascade and long_liq_vol > short_liq_vol:
                side = "SHORT"
                cascade_vol = long_liq_vol
                rationale = (
                    f"Liquidation Cascade: Massive LONG liquidation cascade detected "
                    f"(${long_liq_vol/1e6:.1f}M in {window_sec}s). Joining downward momentum."
                )
            elif is_short_cascade and short_liq_vol > long_liq_vol:
                side = "LONG"
                cascade_vol = short_liq_vol
                rationale = (
                    f"Liquidation Cascade: Massive SHORT liquidation cascade detected "
                    f"(${short_liq_vol/1e6:.1f}M in {window_sec}s). Joining upward momentum."
                )
            else:
                return self._neutral("Conflicting cascade directions")

            # 5. Confidence Calculation
            # Scales with the intensity of the cascade
            # $2M -> 70% Conf, $10M -> 95% Conf
            intensity = cascade_vol / min_liq_usd
            conf = 70.0 + (intensity - 1.0) * 5.0
            conf = max(70.0, min(95.0, conf))

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
                    "cascade_volume_usd": round(cascade_vol, 2),
                    "event_count": len(recent_events),
                    "intensity": round(intensity, 2)
                }
            )

        except Exception as e:
            logger.error("LiquidationCascade error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")