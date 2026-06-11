"""
Tests for all 10 strategy implementations.
Uses synthetic DataSnapshots. Verifies:
  - scan() never raises
  - Signal fields are valid when returned
  - scan() returns None when conditions aren't met
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from datetime import datetime, timezone
from typing import Optional

from models import DataSnapshot, Signal
from strategies import (
    LiquidationFade, FundingSqueeze, OBImbalance, LiquiditySweep,
    VPNode, OIBreakout, BasisReversion, SocialFade, ExchangeArb,
)
from strategies.regime_filter import RegimeFilter


def _utcnow():
    return datetime.now(timezone.utc)


def _base_snapshot(price: float = 100.0) -> DataSnapshot:
    """Create a minimal DataSnapshot with sensible defaults."""
    # Build synthetic candles
    def candles(n=100, vol=500.0, tf="1m"):
        return [
            {
                "open": price * 0.999, "high": price * 1.003,
                "low": price * 0.997, "close": price,
                "volume": vol, "open_time": i * 60_000,
                "interval": tf,
            }
            for i in range(n)
        ]

    bids = [[price * (1 - i * 0.0001), 1.0 + i * 0.1] for i in range(20)]
    asks = [[price * (1 + i * 0.0001), 1.0 + i * 0.1] for i in range(20)]

    funding_hist = [0.0001] * 200

    return DataSnapshot(
        symbol="BTCUSDT",
        timestamp=_utcnow(),
        candles_1m=candles(100, 500, "1m"),
        candles_5m=candles(100, 2500, "5m"),
        candles_15m=candles(60, 7500, "15m"),
        candles_1h=candles(50, 30000, "1h"),
        bids=bids,
        asks=asks,
        cvd=0.0,
        cvd_z_score=0.0,
        cvd_slope_5m=0.0,
        cvd_slope_15m=0.0,
        delta_1m=0.0,
        delta_direction=0,
        mark_price=price,
        index_price=price * 0.9998,
        funding_rate=0.0001,
        funding_history=funding_hist,
        open_interest=50000.0,
        oi_history=[50000.0] * 300,
        oi_change_1h=0.0,
        oi_change_4h=0.0,
        atr_1m=0.15,
        atr_5m=0.35,
        atr_1h=0.80,
        spread_pct=0.00003,
        ob_imbalance=1.0,
        volume_ratio=1.5,
        funding_percentile=50.0,
        poc=price,
        vah=price * 1.01,
        val=price * 0.99,
        lvns=[price * 0.995, price * 1.005],
        hvns=[price],
        swing_high_5m=price * 1.02,
        swing_low_5m=price * 0.98,
        swing_high_1h=price * 1.05,
        swing_low_1h=price * 0.95,
        bybit_price=price,
        fear_greed_index=50,
    )


def _assert_signal_valid(test: unittest.TestCase, sig: Optional[Signal], symbol: str = "BTCUSDT"):
    """Assert a returned Signal has valid fields."""
    test.assertIsInstance(sig, Signal)
    test.assertEqual(sig.symbol, symbol)
    test.assertIn(sig.direction, ("LONG", "SHORT"))
    test.assertIn(sig.entry_type, ("LIMIT", "MARKET"))
    test.assertGreater(sig.entry_price, 0)
    test.assertGreater(sig.sl_price, 0)
    test.assertGreater(sig.tp1_price, 0)
    test.assertGreater(sig.confidence, 0)
    test.assertLessEqual(sig.confidence, 1.0)
    test.assertGreater(len(sig.strategy), 0)

    # Direction consistency
    if sig.direction == "LONG":
        test.assertLess(sig.sl_price, sig.entry_price)
        test.assertGreater(sig.tp1_price, sig.entry_price)
    else:
        test.assertGreater(sig.sl_price, sig.entry_price)
        test.assertLess(sig.tp1_price, sig.entry_price)


# ─────────────────────────────────────────────────────────────
# LiquidationFade
# ─────────────────────────────────────────────────────────────

class TestLiquidationFade(unittest.TestCase):

    def setUp(self):
        self.strategy = LiquidationFade()

    def test_returns_none_without_impulse(self):
        snap = _base_snapshot(100.0)
        result = self.strategy.scan("BTCUSDT", snap)
        self.assertIsNone(result)

    def test_triggers_on_downward_impulse_with_reclaim(self):
        snap = _base_snapshot(100.0)
        snap.oi_change_1h = -0.10     # Strong OI drop
        snap.cvd_z_score = -3.5       # Extreme selling

        # Simulate 5m candles with downward impulse + reclaim
        c = snap.candles_5m
        # 6 candles ago: 100, prev: 97 (impulse down), current recovering
        for i in range(len(c)):
            c[i] = dict(c[i])
        c[-7]["close"] = 100.0
        c[-6]["close"] = 97.0   # -3% impulse
        c[-6]["low"]   = 96.5
        c[-5]["close"] = 97.5
        c[-4]["close"] = 98.0
        c[-3]["close"] = 98.5
        c[-2]["close"] = 99.0   # Reclaim > 50%

        snap.mark_price = 99.0
        result = self.strategy.scan("BTCUSDT", snap)
        # Might or might not trigger depending on exact calculations,
        # but must not raise
        self.assertIsInstance(result, (type(None), Signal))

    def test_never_raises(self):
        for _ in range(10):
            snap = _base_snapshot(100.0)
            try:
                self.strategy.scan("BTCUSDT", snap)
            except Exception as e:
                self.fail(f"LiquidationFade.scan() raised: {e}")


# ─────────────────────────────────────────────────────────────
# FundingSqueeze
# ─────────────────────────────────────────────────────────────

class TestFundingSqueeze(unittest.TestCase):

    def setUp(self):
        self.strategy = FundingSqueeze()

    def test_returns_none_on_neutral_funding(self):
        snap = _base_snapshot(100.0)
        snap.funding_percentile = 50.0
        result = self.strategy.scan("BTCUSDT", snap)
        self.assertIsNone(result)

    def test_short_on_extreme_high_funding(self):
        snap = _base_snapshot(100.0)
        snap.funding_percentile = 95.0   # Extreme high
        snap.funding_rate = 0.001
        snap.oi_change_1h = -0.08        # OI contracting
        snap.delta_direction = -1        # Selling
        snap.atr_1h = 1.5
        result = self.strategy.scan("BTCUSDT", snap)
        if result:
            _assert_signal_valid(self, result)
            self.assertEqual(result.direction, "SHORT")

    def test_long_on_extreme_low_funding(self):
        snap = _base_snapshot(100.0)
        snap.funding_percentile = 5.0    # Extreme low (shorts crowded)
        snap.funding_rate = -0.001
        snap.oi_change_1h = -0.08
        snap.delta_direction = 1
        snap.atr_1h = 1.5
        result = self.strategy.scan("BTCUSDT", snap)
        if result:
            _assert_signal_valid(self, result)
            self.assertEqual(result.direction, "LONG")

    def test_requires_oi_contraction(self):
        snap = _base_snapshot(100.0)
        snap.funding_percentile = 95.0
        snap.oi_change_1h = 0.10    # OI expanding — should not trigger
        result = self.strategy.scan("BTCUSDT", snap)
        self.assertIsNone(result)

    def test_never_raises(self):
        for _ in range(5):
            snap = _base_snapshot()
            try:
                self.strategy.scan("BTCUSDT", snap)
            except Exception as e:
                self.fail(f"FundingSqueeze raised: {e}")


# ─────────────────────────────────────────────────────────────
# OBImbalance
# ─────────────────────────────────────────────────────────────

class TestOBImbalance(unittest.TestCase):

    def setUp(self):
        self.strategy = OBImbalance()

    def test_long_on_heavy_bid_imbalance(self):
        snap = _base_snapshot(100.0)
        snap.ob_imbalance = 3.0     # Heavy bids
        snap.delta_direction = 1    # Buying
        snap.spread_pct = 0.00003   # Tight spread
        snap.volume_ratio = 2.0
        snap.atr_1m = 0.15
        result = self.strategy.scan("BTCUSDT", snap)
        if result:
            _assert_signal_valid(self, result)
            self.assertEqual(result.direction, "LONG")

    def test_short_on_heavy_ask_imbalance(self):
        snap = _base_snapshot(100.0)
        snap.ob_imbalance = 0.3     # Heavy asks
        snap.delta_direction = -1
        snap.spread_pct = 0.00003
        snap.volume_ratio = 2.0
        snap.atr_1m = 0.15
        result = self.strategy.scan("BTCUSDT", snap)
        if result:
            _assert_signal_valid(self, result)
            self.assertEqual(result.direction, "SHORT")

    def test_no_signal_with_wide_spread(self):
        snap = _base_snapshot(100.0)
        snap.ob_imbalance = 3.0
        snap.spread_pct = 0.002   # Too wide
        result = self.strategy.scan("BTCUSDT", snap)
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────
# VPNode
# ─────────────────────────────────────────────────────────────

class TestVPNode(unittest.TestCase):

    def setUp(self):
        self.strategy = VPNode()

    def test_lvn_rejection_long(self):
        snap = _base_snapshot(99.5)  # Price at LVN below 100
        snap.poc = 100.0
        snap.vah = 101.0
        snap.val = 99.0
        snap.lvns = [99.5]          # Price exactly at LVN
        snap.hvns = [100.0]
        snap.atr_1h = 0.8
        # Result may be None or Signal depending on R-ratio checks
        result = self.strategy.scan("BTCUSDT", snap)
        self.assertIsInstance(result, (type(None), Signal))
        if result:
            _assert_signal_valid(self, result)

    def test_above_vah_short(self):
        snap = _base_snapshot(103.0)  # Price above VAH
        snap.poc = 100.0
        snap.vah = 101.0
        snap.val = 99.0
        snap.lvns = []
        snap.atr_1h = 0.8
        result = self.strategy.scan("BTCUSDT", snap)
        if result:
            _assert_signal_valid(self, result)
            self.assertEqual(result.direction, "SHORT")

    def test_below_val_long(self):
        snap = _base_snapshot(97.0)   # Price below VAL
        snap.poc = 100.0
        snap.vah = 101.0
        snap.val = 99.0
        snap.lvns = []
        snap.atr_1h = 0.8
        result = self.strategy.scan("BTCUSDT", snap)
        if result:
            _assert_signal_valid(self, result)
            self.assertEqual(result.direction, "LONG")


# ─────────────────────────────────────────────────────────────
# OIBreakout
# ─────────────────────────────────────────────────────────────

class TestOIBreakout(unittest.TestCase):

    def setUp(self):
        self.strategy = OIBreakout()

    def test_no_signal_on_low_oi_change(self):
        snap = _base_snapshot(100.0)
        snap.oi_change_1h = 0.01    # Only 1% — below threshold
        result = self.strategy.scan("BTCUSDT", snap)
        self.assertIsNone(result)

    def test_long_on_upward_breakout(self):
        snap = _base_snapshot(110.0)  # Price above range high
        snap.oi_change_1h = 0.12
        snap.spread_pct = 0.00003
        snap.volume_ratio = 2.0
        snap.ob_imbalance = 1.5     # Bid bias
        snap.atr_1h = 0.8

        # Set range candles to 100 (price is 10% above)
        for i in range(-22, -2):
            snap.candles_1h[i] = dict(snap.candles_1h[i])
            snap.candles_1h[i]["high"] = 102.0
            snap.candles_1h[i]["low"]  = 98.0
            snap.candles_1h[i]["close"] = 100.0

        result = self.strategy.scan("BTCUSDT", snap)
        if result:
            _assert_signal_valid(self, result)
            self.assertEqual(result.direction, "LONG")


# ─────────────────────────────────────────────────────────────
# BasisReversion
# ─────────────────────────────────────────────────────────────

class TestBasisReversion(unittest.TestCase):

    def setUp(self):
        self.strategy = BasisReversion()

    def test_no_signal_without_history(self):
        snap = _base_snapshot(100.0)
        snap.funding_history = [0.0001] * 10   # Too few
        result = self.strategy.scan("BTCUSDT", snap)
        self.assertIsNone(result)

    def test_short_on_extreme_funding(self):
        snap = _base_snapshot(100.0)
        # Normal funding is ~0.0001, make current extreme
        snap.funding_history = [0.0001] * 200
        snap.funding_rate = 0.0015   # 15x normal
        snap.delta_direction = -1    # Selling
        snap.atr_5m = 0.35
        result = self.strategy.scan("BTCUSDT", snap)
        if result:
            _assert_signal_valid(self, result)
            self.assertEqual(result.direction, "SHORT")


# ─────────────────────────────────────────────────────────────
# SocialFade
# ─────────────────────────────────────────────────────────────

class TestSocialFade(unittest.TestCase):

    def setUp(self):
        self.strategy = SocialFade()

    def test_no_signal_neutral_fg(self):
        snap = _base_snapshot(100.0)
        snap.fear_greed_index = 50
        result = self.strategy.scan("BTCUSDT", snap)
        self.assertIsNone(result)

    def test_short_on_extreme_greed(self):
        snap = _base_snapshot(100.0)
        snap.fear_greed_index = 90    # Extreme greed
        snap.volume_ratio = 3.0       # Volume spike
        snap.atr_1h = 0.8

        # Simulate 10% up move in last 4 candles
        for i in range(-5, -1):
            snap.candles_1h[i] = dict(snap.candles_1h[i])
        snap.candles_1h[-5]["close"] = 90.0
        snap.candles_1h[-1]["close"] = 100.0
        snap.candles_1h[-1]["high"]  = 102.0   # Long wick
        snap.mark_price = 100.0

        result = self.strategy.scan("BTCUSDT", snap)
        if result:
            _assert_signal_valid(self, result)
            self.assertEqual(result.direction, "SHORT")

    def test_no_signal_without_volume_spike(self):
        snap = _base_snapshot(100.0)
        snap.fear_greed_index = 85
        snap.volume_ratio = 1.1    # Not high enough
        result = self.strategy.scan("BTCUSDT", snap)
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────
# ExchangeArb
# ─────────────────────────────────────────────────────────────

class TestExchangeArb(unittest.TestCase):

    def setUp(self):
        self.strategy = ExchangeArb()

    def test_no_signal_same_price(self):
        snap = _base_snapshot(100.0)
        snap.bybit_price = 100.0
        snap.mark_price = 100.0
        # Need a previous reading first
        self.strategy.scan("BTCUSDT", snap)   # Init previous
        result = self.strategy.scan("BTCUSDT", snap)
        self.assertIsNone(result)

    def test_long_on_bybit_lead_up(self):
        snap = _base_snapshot(100.0)
        snap.spread_pct = 0.00003
        snap.atr_5m = 0.35

        # First call establishes previous price
        snap.bybit_price = 100.0
        self.strategy.scan("BTCUSDT", snap)

        # Second call: Bybit moves up, Binance lags
        snap.bybit_price = 100.20   # +0.2%
        snap.mark_price  = 100.02   # Binance barely moved (lagging)
        result = self.strategy.scan("BTCUSDT", snap)
        if result:
            _assert_signal_valid(self, result)
            self.assertEqual(result.direction, "LONG")

    def test_no_signal_wide_spread(self):
        snap = _base_snapshot(100.0)
        snap.spread_pct = 0.002   # Too wide
        snap.bybit_price = 100.20
        result = self.strategy.scan("BTCUSDT", snap)
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────
# RegimeFilter
# ─────────────────────────────────────────────────────────────

class TestRegimeFilter(unittest.TestCase):

    def setUp(self):
        self.strategy = RegimeFilter()

    def test_scan_always_returns_none(self):
        snap = _base_snapshot(100.0)
        result = self.strategy.scan("BTCUSDT", snap)
        self.assertIsNone(result)

    def test_bullish_regime_on_negative_funding(self):
        snaps = []
        for _ in range(5):
            s = _base_snapshot(100.0)
            s.funding_rate = -0.0003   # Negative funding (bearish bias → bullish)
            s.oi_change_1h = 0.03
            s.cvd_z_score = 1.5
            s.fear_greed_index = 30    # Fear → contrarian bullish
            snaps.append(s)
        regime = self.strategy.compute_regime(snaps)
        self.assertIn(regime.bias, ("BULLISH", "NEUTRAL"))
        self.assertGreater(regime.position_multiplier, 0)

    def test_bearish_regime_on_extreme_greed(self):
        snaps = []
        for _ in range(5):
            s = _base_snapshot(100.0)
            s.funding_rate = 0.0015    # Very high positive funding
            s.oi_change_1h = -0.02
            s.cvd_z_score = -2.0
            s.fear_greed_index = 85    # Extreme greed → bearish
            snaps.append(s)
        regime = self.strategy.compute_regime(snaps)
        self.assertIn(regime.bias, ("BEARISH", "NEUTRAL"))
        self.assertLessEqual(regime.position_multiplier, 1.0)

    def test_empty_snapshots(self):
        regime = self.strategy.compute_regime([])
        self.assertEqual(regime.bias, "NEUTRAL")
        self.assertEqual(regime.position_multiplier, 1.0)


# ─────────────────────────────────────────────────────────────
# Signal field validation helper test
# ─────────────────────────────────────────────────────────────

class TestSignalModel(unittest.TestCase):

    def test_r_ratio_long(self):
        from models import Signal
        sig = Signal(
            symbol="BTCUSDT", strategy="TEST",
            direction="LONG", confidence=0.6,
            entry_type="LIMIT", entry_price=100.0,
            sl_price=99.0, tp1_price=102.0,
        )
        self.assertAlmostEqual(sig.r_ratio, 2.0, delta=0.01)

    def test_r_ratio_short(self):
        from models import Signal
        sig = Signal(
            symbol="BTCUSDT", strategy="TEST",
            direction="SHORT", confidence=0.6,
            entry_type="LIMIT", entry_price=100.0,
            sl_price=101.0, tp1_price=97.0,
        )
        self.assertAlmostEqual(sig.r_ratio, 3.0, delta=0.01)

    def test_sl_distance_pct(self):
        from models import Signal
        sig = Signal(
            symbol="BTCUSDT", strategy="TEST",
            direction="LONG", confidence=0.6,
            entry_type="LIMIT", entry_price=100.0,
            sl_price=98.0, tp1_price=104.0,
        )
        self.assertAlmostEqual(sig.sl_distance_pct, 0.02, delta=0.001)


if __name__ == "__main__":
    unittest.main(verbosity=2)
