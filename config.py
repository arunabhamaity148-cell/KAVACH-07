"""
KAVACH-07 — Configuration
Loads from .env, validates, exposes typed Config dataclass.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

def _load_env() -> None:
    """Load .env from cwd or parent directories."""
    for p in [Path.cwd(), Path.cwd().parent]:
        env_file = p / ".env"
        if env_file.exists():
            load_dotenv(env_file)
            return
    # Fallback: load from environment variables already set
    load_dotenv()

_load_env()

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()

def _env_float(key: str, default: float) -> float:
    v = _env(key)
    try:
        return float(v) if v else default
    except ValueError:
        return default

def _env_int(key: str, default: int) -> int:
    v = _env(key)
    try:
        return int(v) if v else default
    except ValueError:
        return default

def _env_bool(key: str, default: bool) -> bool:
    v = _env(key).lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    return default

@dataclass
class Config:
    # ─── Exchange ────────────────────────────────────────────
    BINANCE_API_KEY: str = field(default_factory=lambda: _env("BINANCE_API_KEY"))
    BINANCE_SECRET_KEY: str = field(default_factory=lambda: _env("BINANCE_SECRET_KEY"))
    USE_TESTNET: bool = field(default_factory=lambda: _env_bool("USE_TESTNET", True))

    # ─── Telegram ────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    TELEGRAM_CHAT_ID: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    TELEGRAM_ALERTS: bool = True

    # ─── Trading ─────────────────────────────────────────────
    INITIAL_BALANCE: float = field(default_factory=lambda: _env_float("INITIAL_BALANCE", 1000.0))
    MAX_RISK_PER_TRADE: float = field(default_factory=lambda: _env_float("MAX_RISK_PER_TRADE", 0.005))
    MAX_TOTAL_EXPOSURE: float = field(default_factory=lambda: _env_float("MAX_TOTAL_EXPOSURE", 0.02))

    # ─── Pairs ───────────────────────────────────────────────
    BASE_PAIRS: List[str] = field(default_factory=lambda: [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
        "SUIUSDT", "SEIUSDT", "TIAUSDT", "ARBUSDT", "OPUSDT",
        "PYTHUSDT", "JTOUSDT", "WLDUSDT", "STRKUSDT", "APTUSDT",
        "INJUSDT", "RENDERUSDT", "TAOUSDT", "IMXUSDT", "NEARUSDT",
    ])

    # ─── Timeframes ──────────────────────────────────────────
    TIMEFRAMES: List[str] = field(default_factory=lambda: ["1m", "5m", "15m", "1h"])

    # ─── Strategies ──────────────────────────────────────────
    STRATEGIES: List[str] = field(default_factory=lambda: [
        "LIQUIDATION_FADE",
        "FUNDING_SQUEEZE",
        "OB_IMBALANCE",
        "LIQUIDITY_SWEEP",
        "VP_NODE",
        "OI_BREAKOUT",
        "BASIS_REVERSION",
        "REGIME_FILTER",
        "SOCIAL_FADE",
        "EXCHANGE_ARB",
    ])

    # ─── Risk ────────────────────────────────────────────────
    MAX_SL_PCT: float = field(default_factory=lambda: _env_float("MAX_SL_PCT", 0.08))
    MIN_ATR_PCT: float = field(default_factory=lambda: _env_float("MIN_ATR_PCT", 0.001))
    MAX_DAILY_LOSS: float = field(default_factory=lambda: _env_float("MAX_DAILY_LOSS", 0.05))
    DRAWDOWN_REDUCE_THRESHOLD: float = field(default_factory=lambda: _env_float("DRAWDOWN_REDUCE_THRESHOLD", 0.10))
    DRAWDOWN_HALT_THRESHOLD: float = field(default_factory=lambda: _env_float("DRAWDOWN_HALT_THRESHOLD", 0.15))

    # ─── ML ──────────────────────────────────────────────────
    ML_CONFIDENCE_THRESHOLD: float = field(default_factory=lambda: _env_float("ML_CONFIDENCE_THRESHOLD", 0.55))
    ML_MIN_SAMPLES: int = field(default_factory=lambda: _env_int("ML_MIN_SAMPLES", 100))
    ML_DRIFT_THRESHOLD: float = field(default_factory=lambda: _env_float("ML_DRIFT_THRESHOLD", 0.1))

    # ─── Monitoring ──────────────────────────────────────────
    HOURLY_REPORT: bool = True

    # ─── Logging ─────────────────────────────────────────────
    LOG_LEVEL: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    LOG_FILE: str = field(default_factory=lambda: _env("LOG_FILE", "/var/log/kavach-07.log"))

    # ─── System ──────────────────────────────────────────────
    HEARTBEAT_INTERVAL: int = field(default_factory=lambda: _env_int("HEARTBEAT_INTERVAL", 1))
    SCAN_INTERVAL: int = field(default_factory=lambda: _env_int("SCAN_INTERVAL", 45))
    WS_RECONNECT_DELAY: int = field(default_factory=lambda: _env_int("WS_RECONNECT_DELAY", 5))
    MAX_WS_RECONNECT_DELAY: int = field(default_factory=lambda: _env_int("MAX_WS_RECONNECT_DELAY", 300))

    # ─── Risk State File ─────────────────────────────────────
    RISK_STATE_FILE: str = field(default_factory=lambda: _env("RISK_STATE_FILE", "/opt/kavach-07/data/risk_state.json"))

    # ─── External ────────────────────────────────────────────
    BYBIT_API_URL: str = field(default_factory=lambda: _env("BYBIT_API_URL", "https://api.bybit.com"))

    # ─── Derived (set in validate) ───────────────────────────
    BINANCE_WS_BASE: str = ""
    BINANCE_REST_BASE: str = ""

    def validate(self) -> None:
        """Validate config and set derived values. Exits on fatal errors."""
        errors = []

        if not self.TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN is not set")
        if not self.TELEGRAM_CHAT_ID:
            errors.append("TELEGRAM_CHAT_ID is not set")

        if self.MAX_RISK_PER_TRADE <= 0 or self.MAX_RISK_PER_TRADE > 0.05:
            errors.append(f"MAX_RISK_PER_TRADE={self.MAX_RISK_PER_TRADE} must be 0–5%")

        if self.INITIAL_BALANCE <= 0:
            errors.append(f"INITIAL_BALANCE={self.INITIAL_BALANCE} must be positive")

        if not self.BASE_PAIRS:
            errors.append("BASE_PAIRS list is empty")

        if errors:
            print("CONFIG ERRORS — KAVACH-07 cannot start:")
            for e in errors:
                print(f" ✗ {e}")
            sys.exit(1)

        # Derived URLs
        if self.USE_TESTNET:
            self.BINANCE_WS_BASE = "wss://stream.binancefuture.com"
            self.BINANCE_REST_BASE = "https://testnet.binancefuture.com"
        else:
            self.BINANCE_WS_BASE = "wss://fstream.binance.com"
            self.BINANCE_REST_BASE = "https://fapi.binance.com"

    def summary(self) -> str:
        mode = "TESTNET" if self.USE_TESTNET else "LIVE"
        return (
            f"KAVACH-07 | Mode={mode} | "
            f"Balance=${self.INITIAL_BALANCE:.0f} | "
            f"Pairs={len(self.BASE_PAIRS)} | "
            f"Strategies={len(self.STRATEGIES)} | "
            f"Risk={self.MAX_RISK_PER_TRADE*100:.1f}%/trade"
        )

# Singleton
_config: Config | None = None

def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
        _config.validate()
    return _config
