"""
KAVACH-07 — Data Models
Pure dataclasses and enums. No business logic.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any

# ─────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _new_id() -> str:
    return str(uuid.uuid4())[:12]

# ─────────────────────────────────────────────────────────────
# Market Data
# ─────────────────────────────────────────────────────────────

@dataclass
class Candle:
    symbol: str
    interval: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: datetime
    quote_volume: float = 0.0
    num_trades: int = 0
    taker_buy_base: float = 0.0
    is_closed: bool = True

@dataclass
class OrderbookSnapshot:
    symbol: str
    timestamp: datetime
    bids: List[List[float]] = field(default_factory=list)  # [[price, qty], ...]
    asks: List[List[float]] = field(default_factory=list)

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def spread_pct(self) -> float:
        if not self.bids or self.best_bid == 0:
            return 1.0
        return (self.best_ask - self.best_bid) / self.best_bid

    @property
    def imbalance(self) -> float:
        """Bid volume / Ask volume (top 10 levels). >1 = buy pressure."""
        bid_vol = sum(b[1] for b in self.bids[:10])
        ask_vol = sum(a[1] for a in self.asks[:10])
        if ask_vol < 1e-10:
            return 1.0
        return bid_vol / ask_vol

@dataclass
class FundingData:
    symbol: str
    funding_rate: float
    funding_time: datetime
    mark_price: float
    index_price: float
    timestamp: datetime = field(default_factory=_utcnow)

    @property
    def basis_pct(self) -> float:
        if self.index_price < 1e-10:
            return 0.0
        return (self.mark_price - self.index_price) / self.index_price

@dataclass
class OpenInterestData:
    symbol: str
    open_interest: float
    open_interest_value: float
    timestamp: datetime = field(default_factory=_utcnow)

# ─────────────────────────────────────────────────────────────
# Signal
# ─────────────────────────────────────────────────────────────

@dataclass
class Signal:
    symbol: str
    strategy: str
    direction: str  # LONG or SHORT
    confidence: float  # 0.0–1.0
    entry_type: str  # LIMIT or MARKET
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: Optional[float] = None
    risk_pct: float = 0.005
    rationale: str = ""
    timestamp: datetime = field(default_factory=_utcnow)
    id: str = field(default_factory=_new_id)
    ml_score: float = 0.5
    atr: float = 0.0
    filters_passed: Dict[str, bool] = field(default_factory=dict)

    @property
    def r_ratio(self) -> float:
        sl_dist = abs(self.entry_price - self.sl_price)
        if sl_dist < 1e-10:
            return 0.0
        return abs(self.tp1_price - self.entry_price) / sl_dist

    @property
    def sl_distance_pct(self) -> float:
        if self.entry_price < 1e-10:
            return 0.0
        return abs(self.entry_price - self.sl_price) / self.entry_price

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "direction": self.direction,
            "confidence": round(self.confidence, 4),
            "entry_type": self.entry_type,
            "entry_price": self.entry_price,
            "sl_price": self.sl_price,
            "tp1_price": self.tp1_price,
            "tp2_price": self.tp2_price,
            "risk_pct": self.risk_pct,
            "r_ratio": round(self.r_ratio, 2),
            "atr": self.atr,
            "ml_score": round(self.ml_score, 4),
            "rationale": self.rationale,
            "timestamp": self.timestamp.isoformat(),
        }

# ─────────────────────────────────────────────────────────────
# Position
# ─────────────────────────────────────────────────────────────

@dataclass
class Position:
    id: str
    symbol: str
    direction: str
    entry_price: float
    size: float
    sl_price: float
    tp1_price: float
    tp2_price: Optional[float]
    open_time: datetime
    strategy: str
    confidence: float
    status: str  # OPEN | TP1_HIT | TP2_HIT | SL_HIT | EXPIRED | CLOSED
    pnl: float = 0.0
    unrealized_pnl: float = 0.0
    close_time: Optional[datetime] = None
    close_price: Optional[float] = None
    tp1_hit: bool = False
    trailing_stop: Optional[float] = None
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    expiry: Optional[datetime] = None
    # FIX: Store original reserved risk for accurate exposure tracking
    reserved_risk: float = 0.0

    @property
    def is_open(self) -> bool:
        # FIX: Include TP1_HIT positions in open check
        return self.status in ("OPEN", "TP1_HIT")

    def calc_unrealized_pnl(self, current_price: float) -> float:
        if self.direction == "LONG":
            return (current_price - self.entry_price) * self.size
        return (self.entry_price - current_price) * self.size

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "size": self.size,
            "sl_price": self.sl_price,
            "tp1_price": self.tp1_price,
            "tp2_price": self.tp2_price,
            "open_time": self.open_time.isoformat(),
            "strategy": self.strategy,
            "confidence": self.confidence,
            "status": self.status,
            "pnl": round(self.pnl, 4),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "close_time": self.close_time.isoformat() if self.close_time else None,
            "close_price": self.close_price,
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "reserved_risk": self.reserved_risk,
        }

# ─────────────────────────────────────────────────────────────
# DataSnapshot — complete market state for one symbol
# ─────────────────────────────────────────────────────────────

@dataclass
class DataSnapshot:
    symbol: str
    timestamp: datetime

    # OHLCV
    candles_1m: List[Dict] = field(default_factory=list)
    candles_5m: List[Dict] = field(default_factory=list)
    candles_15m: List[Dict] = field(default_factory=list)
    candles_1h: List[Dict] = field(default_factory=list)

    # Orderbook
    bids: List = field(default_factory=list)
    asks: List = field(default_factory=list)

    # Trade flow / CVD
    cvd: float = 0.0
    cvd_z_score: float = 0.0  # z-score relative to recent history
    cvd_slope_5m: float = 0.0  # normalised linear slope
    cvd_slope_15m: float = 0.0
    delta_1m: float = 0.0  # net delta in last 1m window
    delta_direction: int = 0  # +1 buy, -1 sell, 0 neutral

    # Funding & OI
    mark_price: float = 0.0
    index_price: float = 0.0
    funding_rate: float = 0.0
    funding_history: List[float] = field(default_factory=list)
    open_interest: float = 0.0
    oi_history: List[float] = field(default_factory=list)
    oi_change_1h: float = 0.0
    oi_change_4h: float = 0.0

    # Computed
    atr_1m: float = 0.0
    atr_5m: float = 0.0
    atr_1h: float = 0.0
    spread_pct: float = 0.0
    ob_imbalance: float = 1.0
    volume_ratio: float = 1.0
    funding_percentile: float = 50.0

    # Volume profile (session)
    poc: float = 0.0
    vah: float = 0.0
    val: float = 0.0
    lvns: List[float] = field(default_factory=list)
    hvns: List[float] = field(default_factory=list)

    # Swing levels (for liquidity sweep)
    swing_high_5m: float = 0.0
    swing_low_5m: float = 0.0
    swing_high_1h: float = 0.0
    swing_low_1h: float = 0.0

    # Bybit price (for exchange arb)
    bybit_price: float = 0.0

    # Fear & Greed (global, shared across symbols)
    fear_greed_index: int = 50

    @property
    def mid_price(self) -> float:
        if self.mark_price > 0:
            return self.mark_price
        if self.bids and self.asks:
            return (self.bids[0][0] + self.asks[0][0]) / 2
        return 0.0

    # FIX: Add best_ask and best_bid properties for strategies
    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def basis_pct(self) -> float:
        if self.index_price < 1e-10:
            return 0.0
        return (self.mark_price - self.index_price) / self.index_price

# ─────────────────────────────────────────────────────────────
# Regime
# ─────────────────────────────────────────────────────────────

@dataclass
class RegimeSignal:
    bias: str  # BULLISH | BEARISH | NEUTRAL
    confidence: float
    btc_flow: float = 0.0
    eth_flow: float = 0.0
    avg_funding: float = 0.0
    oi_trend: float = 0.0
    position_multiplier: float = 1.0
    timestamp: datetime = field(default_factory=_utcnow)

# ─────────────────────────────────────────────────────────────
# System Health
# ─────────────────────────────────────────────────────────────

@dataclass
class HealthStatus:
    ws_alive: bool = True
    data_fresh: bool = True
    signals_flowing: bool = True
    no_errors: bool = True
    last_ws_message: float = 0.0
    last_signal_time: float = 0.0
    last_data_update: float = 0.0
    error_count: int = 0
    ws_reconnects: int = 0
    uptime_seconds: float = 0.0
    memory_mb: float = 0.0

    @property
    def all_healthy(self) -> bool:
        return all([self.ws_alive, self.data_fresh, self.signals_flowing, self.no_errors])

# ─────────────────────────────────────────────────────────────
# Risk & Metrics
# ─────────────────────────────────────────────────────────────

@dataclass
class RiskMetrics:
    balance: float = 1000.0
    peak_balance: float = 1000.0
    drawdown: float = 0.0
    daily_pnl: float = 0.0
    daily_start_balance: float = 1000.0
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    total_signals: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    circuit_state: str = "OK"  # OK | REDUCE | HALT
    circuit_reason: str = ""
    paused: bool = False
    halt_until: Optional[float] = None

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    @property
    def profit_factor(self) -> float:
        if abs(self.gross_loss) < 1e-10:
            return 999.0 if self.gross_profit > 0 else 0.0
        return self.gross_profit / self.gross_loss

    def to_dict(self) -> dict:
        return {
            "balance": round(self.balance, 2),
            "peak_balance": round(self.peak_balance, 2),
            "drawdown": round(self.drawdown, 4),
            "daily_pnl": round(self.daily_pnl, 2),
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 2),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "consecutive_losses": self.consecutive_losses,
            "circuit_state": self.circuit_state,
            "circuit_reason": self.circuit_reason,
            "paused": self.paused,
        }

@dataclass
class TradeResult:
    position_id: str
    symbol: str
    strategy: str
    direction: str
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    exit_reason: str  # TP1 | TP2 | SL | EXPIRED | MANUAL
    duration_seconds: float
    r_multiple: float
    confidence: float = 0.0
    timestamp: datetime = field(default_factory=_utcnow)
    # FIX: Store original reserved risk for accurate exposure tracking
    reserved_risk: float = 0.0

    def to_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "size": self.size,
            "pnl": round(self.pnl, 4),
            "exit_reason": self.exit_reason,
            "duration_seconds": round(self.duration_seconds, 1),
            "r_multiple": round(self.r_multiple, 2),
            "confidence": round(self.confidence, 4),
            "timestamp": self.timestamp.isoformat(),
            "reserved_risk": self.reserved_risk,
        }
