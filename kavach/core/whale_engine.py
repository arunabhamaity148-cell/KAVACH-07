"""
KAVACH-07 — Whale Engine
Tracks large on-chain movements via Whale Alert API.
Aggregates directional bias to modify position sizing in Risk Manager.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("kavach.whale_engine")

@dataclass(slots=True)
class WhaleTransaction:
    """Represents a large on-chain transfer."""
    blockchain: str
    symbol: str
    amount_usd: float
    transaction_type: str  # 'exchange_inflow', 'exchange_outflow', 'unknown'
    timestamp: float

class WhaleEngine:
    """
    Monitors high-value on-chain transfers to determine institutional bias.
    Requirement: No fabrication. If API key missing, engine disables itself.
    """

    def __init__(self, config: dict, whale_alert_api_key: str):
        self._cfg = config
        self._api_key = whale_alert_api_key
        
        # Config Extraction
        w_cfg = config["phase2"]["whale"]
        self._bullish_threshold = float(w_cfg.get("bullish_threshold_usd", 20000000.0))
        self._bearish_threshold = float(w_cfg.get("bearish_threshold_usd", 15000000.0))
        self._window_seconds = int(w_cfg.get("window_seconds", 3600))
        self._max_txs = int(w_cfg.get("max_transactions", 2000))
        
        # Internal State
        self._enabled = bool(self._api_key)
        self._transactions: deque[WhaleTransaction] = deque(maxlen=self._max_txs)
        self._bias: str = "NEUTRAL"
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_cursor: Optional[str] = None

        if not self._enabled:
            logger.warning("Whale Engine: WHALE_ALERT_API_KEY missing. Engine DISABLED.")

    async def start(self) -> None:
        """Starts the polling loop if enabled."""
        if not self._enabled:
            return

        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self._running = True
        asyncio.create_task(self._poll_loop())
        logger.info("Whale Engine: Real-time tracking active.")

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        if self._session:
            await self._session.close()
        logger.info("Whale Engine stopped")

    def get_bias(self) -> str:
        """Returns the current calculated bias: BULLISH, BEARISH, or NEUTRAL."""
        if not self._enabled:
            return "NEUTRAL"
        return self._bias

    # ──────────────────────────────────────────────────────────────────────────
    # Logic
    # ──────────────────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Polls Whale Alert API every 60 seconds."""
        while self._running:
            try:
                await self._fetch_whale_alert()
                self._prune_transactions()
                self._compute_bias()
            except Exception as e:
                logger.error("Whale Engine poll error: %s", e)
            
            await asyncio.sleep(60)

    async def _fetch_whale_alert(self) -> None:
        """Fetches transactions from the Whale Alert REST API."""
        url = "https://api.whale-alert.io/v1/transactions"
        params = {
            "api_key": self._api_key,
            "min_value": 500000,  # Minimum $500k to even consider
            "start": int(time.time()) - 120 # Look back 2 minutes
        }
        
        async with self._session.get(url, params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                txs = data.get("transactions", [])
                for tx in txs:
                    self._process_transaction(tx)
            else:
                text = await resp.text()
                logger.error("Whale Alert API Error %d: %s", resp.status, text)

    def _process_transaction(self, tx: dict) -> None:
        """Classifies and stores a transaction."""
        try:
            amount_usd = float(tx.get("amount_usd", 0.0))
            from_owner = tx.get("from", {}).get("owner_type", "unknown")
            to_owner = tx.get("to", {}).get("owner_type", "unknown")
            
            tx_type = "unknown"
            # Logic: Stablecoins moving TO exchange = BULLISH (dry powder)
            # BTC/ETH moving TO exchange = BEARISH (sell pressure)
            # BTC/ETH moving FROM exchange = BULLISH (accumulation)
            
            is_stable = tx.get("symbol") in ("USDT", "USDC", "BUSD", "DAI")
            
            if from_owner != "exchange" and to_owner == "exchange":
                tx_type = "exchange_inflow"
            elif from_owner == "exchange" and to_owner != "exchange":
                tx_type = "exchange_outflow"
            
            self._transactions.append(WhaleTransaction(
                blockchain=tx.get("blockchain", "unknown"),
                symbol=tx.get("symbol", "unknown"),
                amount_usd=amount_usd,
                transaction_type=tx_type,
                timestamp=float(tx.get("timestamp", time.time()))
            ))
        except Exception as e:
            logger.debug("Failed to process whale transaction: %s", e)

    def _prune_transactions(self) -> None:
        """Removes transactions older than the rolling window (Bug #15)."""
        cutoff = time.time() - self._window_seconds
        while self._transactions and self._transactions[0].timestamp < cutoff:
            self._transactions.popleft()

    def _compute_bias(self) -> None:
        """
        Calculates directional bias based on aggregated flows.
        - Bullish: Stable Inflows + BTC/ETH Outflows
        - Bearish: BTC/ETH Inflows
        """
        if not self._transactions:
            self._bias = "NEUTRAL"
            return

        bull_sum = 0.0
        bear_sum = 0.0
        
        stablecoins = {"USDT", "USDC", "BUSD", "DAI"}
        
        for tx in self._transactions:
            if tx.transaction_type == "exchange_inflow":
                if tx.symbol in stablecoins:
                    bull_sum += tx.amount_usd
                else:
                    bear_sum += tx.amount_usd
            elif tx.transaction_type == "exchange_outflow":
                if tx.symbol not in stablecoins:
                    bull_sum += tx.amount_usd

        if bull_sum >= self._bullish_threshold and bull_sum > bear_sum:
            self._bias = "BULLISH"
        elif bear_sum >= self._bearish_threshold and bear_sum > bull_sum:
            self._bias = "BEARISH"
        else:
            self._bias = "NEUTRAL"

        logger.debug("Whale Bias: %s (Bull: $%.1fM, Bear: $%.1fM)", 
                     self._bias, bull_sum/1e6, bear_sum/1e6)