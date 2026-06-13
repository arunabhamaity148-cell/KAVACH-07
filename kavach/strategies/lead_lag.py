"""
KAVACH-07 — Phase 2 Lead-Lag Strategy
Advanced cross-exchange lead-lag tracking between Hyperliquid and Binance.
Uses price velocity and volume confirmation to identify early entry opportunities.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.lead_lag")

class LeadLag(StrategyBase):
    """
    Logic:
    1. Tracks price velocity (rate of change per minute) on Hyperliquid.
    2. Measures divergence between Hyperliquid (Lead) and Binance (Lag).
    3. Requirement: Velocity >= 0.05%/min.
    4. Requirement: Significant volume confirmation on leading exchange.
    5. Cooldown: 120 seconds between signals per symbol to avoid over-trading.
    """

    def __init__(self, config: Dict[str, Any], symbol: str):
        super().__init__(config, symbol)
        
        # Internal state for velocity tracking
        # Stores (timestamp, hl_price, volume)
        self._history: deque[Tuple[float, float, float]] = deque(maxlen=60)
        self._last_signal_time: float = 0.0
        
        # Config parameters
        self._div_thresh = float(self._cfg.get("divergence_threshold", 0.0015))
        self._vel_thresh = float(self._cfg.get("velocity_threshold", 0.0005)) # 0.05%
        self._cooldown = float(self._cfg.get("cooldown_seconds", 120.0))
        self._vol_confirm = bool(self._cfg.get("volume_confirmation", True))
        
        self._sl_pct = float(self._cfg.get("sl_percent", 0.2)) / 100.0
        self._tp_pct = float(self._cfg.get("tp_percent", 0.4)) / 100.0

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        if not md or not md.is_warm:
            return self._neutral("Data engine warming up")

        now = time.time()
        hl_price = md.hl_price
        bn_price = md.price
        
        # 1. Update History
        self._history.append((now, hl_price, md.volume))
        
        # 2. Cooldown Check
        if (now - self._last_signal_time) < self._cooldown:
            return self._neutral(f"In cooldown ({int(self._cooldown - (now - self._last_signal_time))}s)")

        # 3. Velocity Calculation
        # We need at least 10 seconds of data to compute velocity
        if len(self._history) < 10:
            return self._neutral("Insufficient history for velocity tracking")
            
        start_ts, start_px, _ = self._history[0]
        end_ts, end_px, _ = self._history[-1]
        
        time_diff_min = (end_ts - start_ts) / 60.0
        if time_diff_min <= 0:
            return self._neutral("Time increment zero")
            
        # Velocity in % per minute
        velocity = (end_px - start_px) / start_px / time_diff_min
        
        # 4. Divergence Check
        divergence = (hl_price - bn_price) / bn_price
        
        # 5. Signal Logic
        side = "NEUTRAL"
        
        # Bullish Lead: HL moving up fast + HL price > Binance price
        if velocity >= self._vel_thresh and divergence >= self._div_thresh:
            side = "LONG"
        # Bearish Lead: HL moving down fast + HL price < Binance price
        elif velocity <= -self._vel_thresh and divergence <= -self._div_thresh:
            side = "SHORT"
            
        if side == "NEUTRAL":
            return self._neutral(f"Vel: {velocity*100:.3f}%/m, Div: {divergence*100:.3f}%")

        # 6. Volume Confirmation
        if self._vol_confirm:
            avg_vol = sum(x[2] for x in self._history) / len(self._history)
            if md.volume < avg_vol * 1.2: # Must be 20% above recent average
                return self._neutral(f"Volume ({md.volume:.1f}) lacks confirmation vs avg ({avg_vol:.1f})")

        try:
            # 7. Calculate Parameters
            self._last_signal_time = now
            
            # Confidence scales with velocity and divergence
            # Max 95, Base 65
            conf = 65.0 + (abs(velocity) / self._vel_thresh * 5.0) + (abs(divergence) / self._div_thresh * 5.0)
            conf = min(95.0, conf)
            
            entry = bn_price
            if side == "LONG":
                sl = entry * (1.0 - self._sl_pct)
                tp = entry * (1.0 + self._tp_pct)
            else:
                sl = entry * (1.0 + self._sl_pct)
                tp = entry * (1.0 - self._tp_pct)
                
            rationale = (
                f"Lead-Lag: HL Leading {side} with {velocity*100:.3f}%/min velocity. "
                f"Divergence: {divergence*100:.3f}%. Volume confirmed."
            )
            
            return self._create_signal(
                side=side,
                confidence=conf,
                entry=entry,
                stop_loss=sl,
                take_profit=tp,
                rationale=rationale,
                extra_metadata={
                    "velocity_pct_min": round(velocity * 100, 4),
                    "divergence_pct": round(divergence * 100, 4),
                    "hl_price": hl_price,
                    "bn_price": bn_price
                }
            )

        except Exception as e:
            logger.error("LeadLag error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")