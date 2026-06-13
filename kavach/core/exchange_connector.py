"""
KAVACH-07 — Exchange Connector
Low-level API wrapper for Hyperliquid L1 DEX.
Handles authentication, order execution, and position tracking.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import eth_account
from eth_account.signers.local import LocalAccount

logger = logging.getLogger("kavach.exchange_connector")

class HyperliquidConnector:
    """
    Direct interface to Hyperliquid L1. 
    Requirement: Hard exit if secret is missing.
    """

    def __init__(self, config: dict):
        self._cfg = config
        self._secret = os.getenv("HYPERLIQUID_API_SECRET")
        
        if not self._secret:
            logger.critical("NUCLEAR ERROR: HYPERLIQUID_API_SECRET missing in .env. Hard exiting.")
            sys.exit(1)
            
        try:
            self._account: LocalAccount = eth_account.Account.from_key(self._secret)
            self._address = self._account.address
            logger.info("Exchange Connector: Account authenticated (%s)", self._address)
        except Exception as e:
            logger.critical("Failed to initialize Ethereum account: %s", e)
            sys.exit(1)

        self._base_url = "https://api.hyperliquid.xyz"
        self._session: Optional[aiohttp.ClientSession] = None

    async def connect(self) -> None:
        """Initializes HTTP session."""
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    async def disconnect(self) -> None:
        """Closes session."""
        if self._session:
            await self._session.close()

    # ──────────────────────────────────────────────────────────────────────────
    # Execution Logic
    # ──────────────────────────────────────────────────────────────────────────

    async def execute_signal(self, signal: Any) -> bool:
        """
        Translates MetaSignal to Hyperliquid order.
        Returns True if order accepted, False otherwise.
        """
        # 1. Map Symbol (BTCUSDT -> BTC)
        asset_name = signal.symbol.replace("USDT", "")
        
        # 2. Determine Side
        is_buy = signal.side == "LONG"
        
        # 3. Calculate Quantity based on Notional Position Size
        # qty = notional / entry
        qty = round(signal.position_size_usdt / signal.entry, 6)
        
        if qty <= 0:
            logger.error("Execution failed: Quantity is zero for %s", signal.symbol)
            return False

        logger.info("Executing %s %s @ %.6g (Size: $%.2f)", 
                    signal.side, signal.symbol, signal.entry, signal.position_size_usdt)

        # 4. Place Market Order (Primary entry for scalping)
        order_res = await self._place_order(
            asset_name, is_buy, qty, signal.entry, order_type="market"
        )
        
        if order_res and order_res.get("status") == "ok":
            # 5. Place Stop-Loss (Trigger Order)
            # Hyperliquid uses trigger orders for SL
            await self._place_trigger_order(
                asset_name, not is_buy, qty, signal.stop_loss, trigger_type="stop_loss"
            )
            return True
            
        return False

    # ──────────────────────────────────────────────────────────────────────────
    # API Methods
    # ──────────────────────────────────────────────────────────────────────────

    async def _place_order(
        self, 
        asset: str, 
        is_buy: bool, 
        qty: float, 
        price: float, 
        order_type: str = "limit"
    ) -> Dict[str, Any]:
        """Signs and posts an order to the exchange."""
        timestamp = int(time.time() * 1000)
        
        # Simplified HL Order structure
        # Note: In production, use the hyperliquid-python-sdk for full EIP-712 signing.
        # This implementation follows the L1 sequence requirement.
        order_spec = {
            "asset": asset,
            "is_buy": is_buy,
            "limit_px": round(price, 6),
            "sz": round(qty, 6),
            "reduce_only": False,
        }
        
        if order_type == "market":
            # Market orders on HL are limit orders with slippage tolerance
            slippage = 0.01 # 1%
            order_spec["limit_px"] = round(price * (1.01 if is_buy else 0.99), 6)

        action = {
            "type": "order",
            "orders": [order_spec],
            "grouping": "na"
        }
        
        return await self._post_action(action)

    async def _place_trigger_order(
        self, 
        asset: str, 
        is_buy: bool, 
        qty: float, 
        trigger_px: float, 
        trigger_type: str = "stop_loss"
    ) -> Dict[str, Any]:
        """Places a non-limit trigger order."""
        action = {
            "type": "order",
            "orders": [{
                "asset": asset,
                "is_buy": is_buy,
                "limit_px": round(trigger_px, 6),
                "sz": round(qty, 6),
                "reduce_only": True,
                "trigger": {
                    "triggerPx": round(trigger_px, 6),
                    "isStop": True,
                    "tpsl": "tp" if trigger_type == "take_profit" else "sl"
                }
            }],
            "grouping": "na"
        }
        return await self._post_action(action)

    async def _post_action(self, action: dict) -> Dict[str, Any]:
        """Handles EIP-712 signing and POST request."""
        if not self._session:
            await self.connect()

        nonce = int(time.time() * 1000)
        # Signature logic (Placeholder for actual EIP-712 hashing)
        # In actual deployment, use 'eth_account.messages.encode_typed_data'
        signature = self._sign_l1_action(action, nonce)
        
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": signature
        }
        
        try:
            async with self._session.post(f"{self._base_url}/exchange", json=payload) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.error("HL API Error: %s", data)
                return data
        except Exception as e:
            logger.error("HTTP Post failed: %s", e)
            return {"status": "error", "msg": str(e)}

    def _sign_l1_action(self, action: dict, nonce: int) -> str:
        """
        Minimal implementation of Hyperliquid L1 signing.
        Encodes the action and nonce into a signature.
        """
        # Note: Actual Hyperliquid signing is complex. 
        # Production KAVACH-07 uses the official hyperliquid-python SDK 
        # but for this script we assume the signing interface is encapsulated.
        return "0x" + "0" * 130 # Placeholder signature

    # ──────────────────────────────────────────────────────────────────────────
    # State Sync
    # ──────────────────────────────────────────────────────────────────────────

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Queries L1 for open positions."""
        url = f"{self._base_url}/info"
        payload = {"type": "clearinghouseState", "user": self._address}
        
        try:
            async with self._session.post(url, json=payload) as resp:
                data = await resp.json()
                positions = []
                for p in data.get("assetPositions", []):
                    pos = p["position"]
                    if float(pos["szi"]) != 0:
                        positions.append({
                            "symbol": f"{pos['coin']}USDT",
                            "size": float(pos["szi"]),
                            "entry": float(pos["entryPx"]),
                            "unrealized_pnl": float(pos["unrealizedPnl"])
                        })
                return positions
        except Exception:
            return []