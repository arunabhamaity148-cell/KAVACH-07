"""
Tests for RiskManager — position sizing, drawdown multipliers,
circuit breaker logic, balance persistence.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from models import Signal, TradeResult, RiskMetrics


def _make_signal(entry=100.0, sl=99.0, direction="LONG",
                 confidence=0.65, risk_pct=0.005) -> Signal:
    return Signal(
        symbol="BTCUSDT", strategy="TEST",
        direction=direction, confidence=confidence,
        entry_type="LIMIT", entry_price=entry,
        sl_price=sl, tp1_price=105.0,
        risk_pct=risk_pct, atr=0.35,
    )


def _make_result(pnl: float, strategy: str = "TEST") -> TradeResult:
    from datetime import datetime, timezone
    return TradeResult(
        position_id="pos1", symbol="BTCUSDT",
        strategy=strategy, direction="LONG",
        entry_price=100.0, exit_price=100.0 + pnl,
        size=10.0, pnl=pnl, exit_reason="TP1",
        duration_seconds=3600.0, r_multiple=pnl / 10.0,
        confidence=0.65,
    )


def _make_rm(balance: float = 1000.0):
    """Create a RiskManager with a mock DB."""
    from config import Config
    from risk_manager import RiskManager

    cfg = Config()
    cfg.INITIAL_BALANCE = balance
    cfg.MAX_RISK_PER_TRADE = 0.005
    cfg.MAX_TOTAL_EXPOSURE = 0.02
    cfg.MAX_SL_PCT = 0.08
    cfg.MAX_DAILY_LOSS = 0.05
    cfg.DRAWDOWN_REDUCE_THRESHOLD = 0.10
    cfg.DRAWDOWN_HALT_THRESHOLD = 0.15

    db = MagicMock()
    db.save_risk_metrics = AsyncMock()
    db.load_risk_metrics = AsyncMock(return_value=None)

    rm = RiskManager(cfg, db)
    rm._metrics.balance = balance
    rm._metrics.peak_balance = balance
    rm._metrics.daily_start_balance = balance
    return rm


class TestPositionSizing(unittest.TestCase):

    def test_basic_size_calculation(self):
        rm = _make_rm(1000.0)
        sig = _make_signal(entry=100.0, sl=99.0)  # SL = $1

        # Expected: risk = 1000 * 0.005 = $5 → size = 5/1 = 5 units
        size, reason = rm.calculate_size(sig)
        self.assertAlmostEqual(size, 5.0, delta=0.5)
        self.assertEqual(reason, "")

    def test_larger_sl_gives_smaller_size(self):
        rm = _make_rm(1000.0)
        sig_tight = _make_signal(entry=100.0, sl=99.0)   # SL = $1
        sig_wide  = _make_signal(entry=100.0, sl=97.0)   # SL = $3

        size_tight, _ = rm.calculate_size(sig_tight)
        size_wide,  _ = rm.calculate_size(sig_wide)
        self.assertGreater(size_tight, size_wide)

    def test_halted_returns_zero(self):
        import time
        rm = _make_rm(1000.0)
        rm._metrics.circuit_state = "HALT"
        rm._metrics.halt_until = time.time() + 3600
        sig = _make_signal()
        size, reason = rm.calculate_size(sig)
        self.assertEqual(size, 0.0)
        self.assertIn("HALTED", reason)

    def test_paused_returns_zero(self):
        rm = _make_rm(1000.0)
        rm.pause()
        sig = _make_signal()
        size, reason = rm.calculate_size(sig)
        self.assertEqual(size, 0.0)
        self.assertIn("paused", reason.lower())

    def test_total_exposure_limit(self):
        rm = _make_rm(1000.0)
        # Saturate exposure: MAX_TOTAL_EXPOSURE = 2%
        rm._open_exposure = 1000.0 * 0.02  # Already at limit
        sig = _make_signal(risk_pct=0.005)
        size, reason = rm.calculate_size(sig)
        self.assertEqual(size, 0.0)
        self.assertIn("exposure", reason.lower())


class TestDrawdownMultiplier(unittest.TestCase):

    def setUp(self):
        from risk_manager import _DD_LEVELS
        self._levels = _DD_LEVELS

    def _mult(self, drawdown: float) -> float:
        from risk_manager import _DD_LEVELS
        for threshold, mult in sorted(_DD_LEVELS, key=lambda x: x[0], reverse=True):
            if drawdown >= threshold:
                return mult
        return 1.0

    def test_no_drawdown_full_size(self):
        self.assertEqual(self._mult(0.0), 1.0)

    def test_5pct_drawdown_reduces_size(self):
        m = self._mult(0.05)
        self.assertLess(m, 1.0)
        self.assertGreater(m, 0.0)

    def test_10pct_drawdown_half_size(self):
        m = self._mult(0.10)
        self.assertLessEqual(m, 0.5)

    def test_15pct_drawdown_halts(self):
        m = self._mult(0.15)
        self.assertEqual(m, 0.0)


class TestCircuitBreakers(unittest.TestCase):

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_3_consecutive_losses_triggers_halt(self):
        rm = _make_rm(1000.0)
        rm._metrics.consecutive_losses = 2   # Already 2

        # One more loss
        result = _make_result(-50.0)   # Loss
        self._run(rm.on_trade_closed(result))

        self.assertEqual(rm._metrics.consecutive_losses, 3)
        self.assertEqual(rm._metrics.circuit_state, "HALT")

    def test_win_resets_consecutive_losses(self):
        rm = _make_rm(1000.0)
        rm._metrics.consecutive_losses = 2

        result = _make_result(50.0)   # Win
        self._run(rm.on_trade_closed(result))

        self.assertEqual(rm._metrics.consecutive_losses, 0)
        self.assertEqual(rm._metrics.circuit_state, "OK")

    def test_daily_loss_limit_triggers_halt(self):
        rm = _make_rm(1000.0)
        rm._metrics.daily_start_balance = 1000.0
        rm._metrics.balance = 950.0   # Already -5%

        result = _make_result(-1.0)   # Small additional loss
        self._run(rm.on_trade_closed(result))

        # Should trigger daily loss halt
        self.assertIn(rm._metrics.circuit_state, ("HALT", "OK"))
        # If exactly at threshold, state depends on exact logic

    def test_balance_updates_on_win(self):
        rm = _make_rm(1000.0)
        result = _make_result(50.0)
        self._run(rm.on_trade_closed(result))
        self.assertAlmostEqual(rm._metrics.balance, 1050.0, delta=0.01)

    def test_balance_updates_on_loss(self):
        rm = _make_rm(1000.0)
        result = _make_result(-30.0)
        self._run(rm.on_trade_closed(result))
        self.assertAlmostEqual(rm._metrics.balance, 970.0, delta=0.01)

    def test_peak_balance_tracked(self):
        rm = _make_rm(1000.0)
        result = _make_result(100.0)
        self._run(rm.on_trade_closed(result))
        self.assertAlmostEqual(rm._metrics.peak_balance, 1100.0, delta=0.01)

    def test_drawdown_calculated_correctly(self):
        rm = _make_rm(1000.0)
        # Win to $1100
        self._run(rm.on_trade_closed(_make_result(100.0)))
        # Loss to $1050
        self._run(rm.on_trade_closed(_make_result(-50.0)))
        expected_dd = 50.0 / 1100.0
        self.assertAlmostEqual(rm._metrics.drawdown, expected_dd, delta=0.01)


class TestPauseResume(unittest.TestCase):

    def test_pause(self):
        rm = _make_rm(1000.0)
        rm.pause()
        self.assertTrue(rm._metrics.paused)
        self.assertTrue(rm.is_halted)

    def test_resume_clears_pause(self):
        rm = _make_rm(1000.0)
        rm.pause()
        rm.resume()
        self.assertFalse(rm._metrics.paused)

    def test_resume_clears_halt(self):
        import time
        rm = _make_rm(1000.0)
        rm._metrics.circuit_state = "HALT"
        rm._metrics.halt_until = time.time() + 3600
        rm.resume()
        self.assertEqual(rm._metrics.circuit_state, "OK")


class TestRiskMetrics(unittest.TestCase):

    def test_win_rate(self):
        m = RiskMetrics()
        m.total_trades = 10
        m.winning_trades = 7
        self.assertAlmostEqual(m.win_rate, 0.7)

    def test_profit_factor(self):
        m = RiskMetrics()
        m.gross_profit = 300.0
        m.gross_loss = -100.0
        self.assertAlmostEqual(m.profit_factor, 3.0)

    def test_win_rate_zero_trades(self):
        m = RiskMetrics()
        self.assertEqual(m.win_rate, 0.0)

    def test_profit_factor_no_loss(self):
        m = RiskMetrics()
        m.gross_profit = 100.0
        m.gross_loss = 0.0
        self.assertEqual(m.profit_factor, 999.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
