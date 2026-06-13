"""
KAVACH-07 — Tokenized Security Strategy
Signals directional trades by analyzing the correlation between the crypto asset
and a macro benchmark (typically BTC or a TradFi proxy if available).
Logic: High correlation (>0.6) indicates the asset is following macro trends.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np
from kavach.strategies.base import Signal, StrategyBase

logger = logging.getLogger("kavach.strategies.tokenized_security")

class TokenizedSecurity(StrategyBase):
    """
    Logic:
    1. Retrieve the price series for the current symbol and the benchmark (BTCUSDT).
    2. Calculate the Pearson correlation coefficient over the correlation_period (default 24h).
    3. If correlation >= correlation_threshold (default 0.6):
       - The asset is in a 'Macro-Following' regime.
       - Signal LONG if the benchmark has a positive returns profile over the period.
       - Signal SHORT if the benchmark has a negative returns profile over the period.
    4. If correlation < 0.6, return NEUTRAL (Asset is decoupling/idiosyncratic).
    """

    async def generate_signal(self, data_context: Dict[str, Any]) -> Signal:
        md = data_context.get(self.symbol)
        benchmark_md = data_context.get("BTCUSDT")

        if not md or not md.is_warm or not benchmark_md or not benchmark_md.is_warm:
            return self._neutral("Data engine or benchmark not warm")

        # Config parameters
        threshold = float(self._cfg.get("correlation_threshold", 0.6))
        period = int(self._cfg.get("correlation_period", 24)) # Typically hours
        sl_pct = float(self._cfg.get("sl_percent", 1.0)) / 100.0
        tp_pct = float(self._cfg.get("tp_percent", 2.0)) / 100.0

        try:
            # We need 1m klines for the correlation calculation. 
            # period * 60 = minutes.
            klines_needed = period * 60
            if len(md.klines_1m) < klines_needed or len(benchmark_md.klines_1m) < klines_needed:
                # Fallback to available data if at least 1/4 of the period is present
                klines_needed = max(60, min(klines_needed, len(md.klines_1m), len(benchmark_md.klines_1m)))

            # Extract Close prices
            target_series = np.array([k[4] for k in list(md.klines_1m)[-klines_needed:]])
            bench_series = np.array([k[4] for k in list(benchmark_md.klines_1m)[-klines_needed:]])

            if len(target_series) < 10:
                return self._neutral("Insufficient kline series length")

            # 1. Calculate Pearson Correlation
            # Formula: cov(X,Y) / (sigma_x * sigma_y)
            correlation_matrix = np.corrcoef(target_series, bench_series)
            correlation = correlation_matrix[0, 1]

            if np.isnan(correlation) or correlation < threshold:
                return self._neutral(f"Low macro correlation ({correlation:.2f})")

            # 2. Analyze Benchmark Trend
            # Calculate simple returns over the period for the benchmark
            bench_start_price = bench_series[0]
            bench_end_price = bench_series[-1]
            bench_return = (bench_end_price - bench_start_price) / bench_start_price

            # 3. Determine Side
            # If high correlation, we follow the benchmark's direction
            if bench_return > 0.001: # Min 0.1% move to avoid noise
                side = "LONG"
                direction_str = "BULLISH"
            elif bench_return < -0.001:
                side = "SHORT"
                direction_str = "BEARISH"
            else:
                return self._neutral("Benchmark trend is flat")

            # 4. Confidence Calculation
            # Scales with correlation strength and benchmark return magnitude
            # Base 60% + (Corr - Threshold) bonus + Return bonus
            conf = 60.0 + (correlation - threshold) * 50.0 + (abs(bench_return) * 100.0)
            conf = max(60.0, min(90.0, conf))

            entry = md.price
            if side == "LONG":
                sl = entry * (1.0 - sl_pct)
                tp = entry * (1.0 + tp_pct)
            else:
                sl = entry * (1.0 + sl_pct)
                tp = entry * (1.0 - tp_pct)

            rationale = (
                f"Tokenized Security: High correlation ({correlation:.2f}) with BTC. "
                f"Benchmark is {direction_str} ({bench_return*100:.2f}% return). "
                f"Anticipating asset to continue following macro lead."
            )

            return self._create_signal(
                side=side,
                confidence=conf,
                entry=entry,
                stop_loss=sl,
                take_profit=tp,
                rationale=rationale,
                extra_metadata={
                    "correlation": round(correlation, 4),
                    "benchmark_return": round(bench_return, 4),
                    "period_hours": round(klines_needed / 60, 1)
                }
            )

        except Exception as e:
            logger.error("TokenizedSecurity error for %s: %s", self.symbol, e)
            return self._neutral(f"Execution error: {str(e)}")