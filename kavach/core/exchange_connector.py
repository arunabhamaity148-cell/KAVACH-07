"""
KAVACH-07 — Exchange Connector (REMEDIATED)
Fixed: Placeholder signature replaced with real EIP-712 signing logic.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

import aiohttp
import eth_account
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

logger = logging.getLogger("kavach.exchange_connector")

class HyperliquidConnector:
    """
    Direct interface to Hyperliquid L1. 
    Uses official SDK for secure EIP-712 signing.
    """

    def __init__(self, config: dict):
        self._cfg = config
        self._secret = os.getenv("HYPERLIQUID_API_SECRET")
        
        if not self._secret:
            logger.critical("FATAL: HYPERLIQUID_API_SECRET missing in .env. Hard exiting.")
            sys.exit(1)
            
        try:
            self._account: LocalAccount = eth_account.Account.from_key(self._secret)
            self._address = self._account.address
            
            # Initialize official SDK components for real execution
            self._info = Info(constants.MAINNET_API_URL, skip_latency_check=True)
            self._exchange = Exchange(self._account, constants.MAINNET_API_URL)
            
            logger.info("Exchange Connector: Account authenticated (%s)", self._address)
        except Exception as e:
            logger.critical("Failed to initialize Hyperliquid SDK: %s", e)
            sys.exit(1)

    async def connect(self) -> None:
        """SDK components manage their own sessions, but we log status."""
        logger.info("Exchange Connector connected to Hyperliquid Mainnet")

    async def disconnect(self) -> None:
        """Cleanup logic."""
        pass

    async def execute_signal(self, signal: Any) -> bool:
        """
        Translates MetaSignal to real Hyperliquid orders.
        Logic: Place Market Order + Immediate Stop Loss.
        """
        asset = signal.symbol.replace("USDT", "")
        is_buy = signal.side == "LONG"
        
        # Calculate precise quantity
        qty = round(signal.position_size_usdt / signal.entry, 6)
        
        if qty <= 0:
            logger.error("Execution failed: Quantity too small for %s", signal.symbol)
            return False

        try:
            logger.info("Executing %s %s | Size: $%.2f", signal.side, asset, signal.position_size_usdt)

            # 1. Market Order Execution
            # In HL, Market orders are Market-Limit with 1% slippage for safety
            order_res = self._exchange.market_open(asset, is_buy, qty, px=None, slippage=0.01)
            
            if order_res["status"] == "ok":
                order_data = order_res["response"]["data"]["statuses"][0]
                if "filled" in order_data:
                    avg_px = float(order_data["filled"]["avgPx"])
                    logger.info("✓ Order Filled @ %.6g", avg_px)
                    
                    # 2. Place Real Stop Loss
                    # reduces current position if triggered
                    self._exchange.stop_loss(asset, not is_buy, qty, signal.stop_loss)
                    return True
                else:
                    logger.error("Order accepted but not filled: %s", order_data)
            else:
                logger.error("Order rejected by Hyperliquid: %s", order_res)
                
            return False
            
        except Exception as e:
            logger.error("Execution error for %s: %s", asset, e)
            return False

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Fetch live positions from exchange to prevent desync."""
        try:
            user_state = self._info.user_state(self._address)
            positions = []
            for p in user_state.get("assetPositions", []):
                pos = p["position"]
                if float(pos["szi"]) != 0:
                    positions.append({
                        "symbol": f"{pos['coin']}USDT",
                        "size": float(pos["szi"]),
                        "entry": float(pos["entryPx"]),
                        "unrealized_pnl": float(pos["unrealizedPnl"])
                    })
            return positions
        except Exception as e:
            logger.error("Failed to fetch positions: %s", e)
            return []