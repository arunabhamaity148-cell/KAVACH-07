"""
KAVACH-07 — Risk Manager
Position sizing, circuit breakers, and capital protection.
All state persisted to disk.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from config import Config
from utils import get_logger

logger = get_logger(__name__)


@dataclass
class Metrics:
    total_signals: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    peak_equity: float = 0.0
    max_drawdown: float = 0.0
    daily_pnl: float = 0.0
    last_reset_day: int = 0
    consecutive_losses: int = 0
    circuit_breaker_state: str = "OK"
    circuit_breaker_reason: str = ""


class RiskManager:
    def __init__(self, config: Config):
        self._cfg = config
        self._balance = config.INITIAL_BALANCE
        self._metrics = Metrics()
        self._metrics.peak_equity = self._balance
        self._metrics.last_reset_day = self._today()

        self._state_path = config.RISK_STATE_FILE
        self._load_state()

    # ─── State persistence ───────────────────────────────────

    def _load_state(self) -> None:
        if not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, "r") as f:
                data = json.load(f)
            self._balance = data.get("balance", self._balance)
            self._metrics.total_signals = data.get("total_signals", 0)
            self._metrics.total_trades = data.get("total_trades", 0)
            self._metrics.winning_trades = data.get("winning_trades", 0)
            self._metrics.losing_trades = data.get("losing_trades", 0)
            self._metrics.total_pnl = data.get("total_pnl", 0.0)
            self._metrics.peak_equity = data.get("peak_equity", self._balance)
            self._metrics.max_drawdown = data.get("max_drawdown", 0.0)
            self._metrics.daily_pnl = data.get("daily_pnl", 0.0)
            self._metrics.last_reset_day = data.get("last_reset_day", self._today())
            self._metrics.consecutive_losses = data.get("consecutive_losses", 0)
            self._metrics.circuit_breaker_state = data.get("circuit_breaker_state", "OK")
            self._metrics.circuit_breaker_reason = data.get("circuit_breaker_reason", "")
            logger.info(
                f"Risk state loaded: balance=${self._balance:.2f}, "
                f"drawdown={self._metrics.max_drawdown:.1f}%, trades={self._metrics.total_trades}"
            )
        except Exception as e:
            logger.error(f"Failed to load risk state: {e}")

    def _save_state(self) -> None:
        try:
            data = {
                "balance": self._balance,
                "total_signals": self._metrics.total_signals,
                "total_trades": self._metrics.total_trades,
                "winning_trades": self._metrics.winning_trades,
                "losing_trades": self._metrics.losing_trades,
                "total_pnl": self._metrics.total_pnl,
                "peak_equity": self._metrics.peak_equity,
                "max_drawdown": self._metrics.max_drawdown,
                "daily_pnl": self._metrics.daily_pnl,
                "last_reset_day": self._metrics.last_reset_day,
                "consecutive_losses": self._metrics.consecutive_losses,
                "circuit_breaker_state": self._metrics.circuit_breaker_state,
                "circuit_breaker_reason": self._metrics.circuit_breaker_reason,
            }
            with open(self._state_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save risk state: {e}")

    # ─── Public API ─────────────────────────────────────────

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def metrics(self) -> Metrics:
        return self._metrics

    def increment_signals(self, count: int = 1) -> None:
        """Increment signal count by actual valid signals (not scans)"""
        self._metrics.total_signals += count

    def increment_trades(self, pnl: float) -> None:
        self._metrics.total_trades += 1
        self._metrics.total_pnl += pnl
        self._balance += pnl

        if pnl > 0:
            self._metrics.winning_trades += 1
            self._metrics.consecutive_losses = 0
        else:
            self._metrics.losing_trades += 1
            self._metrics.consecutive_losses += 1

        if self._balance > self._metrics.peak_equity:
            self._metrics.peak_equity = self._balance

        dd = (self._metrics.peak_equity - self._balance) / self._metrics.peak_equity
        if dd > self._metrics.max_drawdown:
            self._metrics.max_drawdown = dd

        self._metrics.daily_pnl += pnl
        self._save_state()

    def reset_daily(self) -> None:
        today = self._today()
        if today != self._metrics.last_reset_day:
            self._metrics.daily_pnl = 0.0
            self._metrics.last_reset_day = today
            self._save_state()

    def check_circuit_breakers(self) -> tuple[str, str]:
        cfg = self._cfg

        if self._metrics.consecutive_losses >= cfg.CIRCUIT_CONSECUTIVE_LOSSES:
            return "HALT", f"Consecutive losses: {self._metrics.consecutive_losses}"

        if self._metrics.max_drawdown >= cfg.CIRCUIT_DRAWDOWN_PCT:
            return "HALT", f"Max drawdown: {self._metrics.max_drawdown:.1%}"

        if abs(self._metrics.daily_pnl) / self._cfg.INITIAL_BALANCE >= cfg.CIRCUIT_DAILY_LOSS_PCT:
            return "HALT", f"Daily loss: {self._metrics.daily_pnl:.2f}"

        return "OK", ""

    def set_circuit_state(self, state: str, reason: str) -> None:
        self._metrics.circuit_breaker_state = state
        self._metrics.circuit_breaker_reason = reason
        self._save_state()

    def calculate_position_size(self, balance: float, risk_pct: float, sl_distance: float) -> float:
        if sl_distance <= 0:
            return 0.0
        risk_amount = balance * risk_pct
        return risk_amount / sl_distance

    def _today(self) -> int:
        return int(time.time() // 86400)
