"""
KAVACH-07 — Configuration
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)

def _env_bool(key: str, default: bool = False) -> bool:
    v = _env(key, str(default).lower())
    # FIX: Strip whitespace before comparison
    v = v.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off", ""):
        return False
    return default

def _env_float(key: str, default: float = 0.0) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default

def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default

def _env_list(key: str, default: List[str] = None) -> List[str]:
    if default is None:
        default = []
    val = _env(key, ",".join(default))
    return [x.strip() for x in val.split(",") if x.strip()]

@dataclass
class Config:
    BINANCE_API_KEY: str = field(default_factory=lambda: _env("BINANCE_API_KEY", ""))
    BINANCE_SECRET_KEY: str = field(default_factory=lambda: _env("BINANCE_SECRET_KEY", ""))
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN", ""))
    TELEGRAM_CHAT_ID: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID", ""))
    USE_TESTNET: bool = field(default_factory=lambda: _env_bool("USE_TESTNET", True))
    PAPER_TRADE: bool = field(default_factory=lambda: _env_bool("PAPER_TRADE", True))
    INITIAL_BALANCE: float = field(default_factory=lambda: _env_float("INITIAL_BALANCE", 1000.0))
    MAX_RISK_PER_TRADE: float = field(default_factory=lambda: _env_float("MAX_RISK_PER_TRADE", 0.005))
    MAX_TOTAL_EXPOSURE: float = field(default_factory=lambda: _env_float("MAX_TOTAL_EXPOSURE", 0.02))
    MAX_DAILY_LOSS: float = field(default_factory=lambda: _env_float("MAX_DAILY_LOSS", 0.05))
    DRAWDOWN_REDUCE_THRESHOLD: float = field(default_factory=lambda: _env_float("DRAWDOWN_REDUCE_THRESHOLD", 0.05))
    DRAWDOWN_HALT_THRESHOLD: float = field(default_factory=lambda: _env_float("DRAWDOWN_HALT_THRESHOLD", 0.15))
    SCAN_INTERVAL: int = field(default_factory=lambda: _env_int("SCAN_INTERVAL", 45))
    HEARTBEAT_INTERVAL: int = field(default_factory=lambda: _env_int("HEARTBEAT_INTERVAL", 1))
    BASE_PAIRS: List[str] = field(default_factory=lambda: _env_list("BASE_PAIRS", [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "MATICUSDT",
        "DOTUSDT", "LTCUSDT", "UNIUSDT", "ATOMUSDT", "ETCUSDT",
        "FILUSDT", "ALGOUSDT", "VETUSDT", "TRXUSDT", "ICPUSDT",
        "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT",
    ]))
    TIMEFRAMES: List[str] = field(default_factory=lambda: ["1m", "5m", "15m", "1h"])
    STRATEGIES: List[str] = field(default_factory=lambda: _env_list("STRATEGIES", [
        "LIQUIDATION_FADE", "FUNDING_SQUEEZE", "OB_IMBALANCE",
        "LIQUIDITY_SWEEP", "VP_NODE", "OI_BREAKOUT",
        "BASIS_REVERSION", "REGIME_FILTER", "SOCIAL_FADE", "EXCHANGE_ARB",
    ]))
    WS_BASE: str = field(default_factory=lambda: _env("WS_BASE", "wss://fstream.binance.com"))
    REST_BASE: str = field(default_factory=lambda: _env("REST_BASE", "https://fapi.binance.com/fapi/v1"))
    # FIX: Use relative path for LOG_FILE
    LOG_FILE: str = field(default_factory=lambda: _env("LOG_FILE", "logs/kavach-07.log"))
    DB_PATH: str = field(default_factory=lambda: _env("DB_PATH", "kavach07.db"))
    LOG_LEVEL: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    _start_time: float = 0.0

    def validate(self) -> List[str]:
        errors = []
        if not self.TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN is required")
        if not self.TELEGRAM_CHAT_ID:
            errors.append("TELEGRAM_CHAT_ID is required")
        if not self.BASE_PAIRS:
            errors.append("BASE_PAIRS cannot be empty")
        if self.MAX_RISK_PER_TRADE <= 0 or self.MAX_RISK_PER_TRADE > 0.1:
            errors.append("MAX_RISK_PER_TRADE must be between 0 and 0.1")
        if self.MAX_TOTAL_EXPOSURE <= 0 or self.MAX_TOTAL_EXPOSURE > 0.5:
            errors.append("MAX_TOTAL_EXPOSURE must be between 0 and 0.5")
        return errors
