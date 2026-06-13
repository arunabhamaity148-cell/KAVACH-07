"""
KAVACH-07 — Hyperliquid Signing Utility
Implements EIP-712 structured data hashing and signing for the Hyperliquid L1.
Required to satisfy the 'No Placeholders' rule for the Exchange Connector.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from eth_abi import encode
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_bytes

class SigningHelper:
    """
    Handles the cryptographic construction of Hyperliquid L1 transactions.
    Calculates the L1 action hash and signs it using the user's private key.
    """

    @staticmethod
    def sign_l1_action(wallet: Any, action: Dict[str, Any], nonce: int, is_mainnet: bool = True) -> str:
        """
        Signs a Hyperliquid L1 action. 
        Note: wallet is a LocalAccount instance from eth_account.
        """
        # 1. Serialize the action to JSON (compact)
        data = json.dumps(action, separators=(",", ":"))
        data_hash = keccak(to_bytes(text=data))
        
        # 2. Encode the (hash, nonce) for the domain
        # The domain separator for Hyperliquid L1
        domain = {
            "name": "Exchange",
            "version": "1",
            "chainId": 1337, # Hyperliquid specific ChainID
            "verifyingContract": "0x0000000000000000000000000000000000000000"
        }

        # 3. Define the Types for EIP-712
        # Hyperliquid uses a 'Agent' or 'Exchange' type depending on the operation
        # For simple orders, we use the standard Hashing format
        types = {
            "Agent": [
                {"name": "source", "type": "string"},
                {"name": "connectionId", "type": "bytes32"}
            ]
        }

        # 4. Construct the structured message
        # Hyperliquid unique hashing: concatenates the data hash with the nonce
        # then treats it as a connectionId
        connection_id = keccak(data_hash + encode(['uint64'], [nonce]))
        
        message = {
            "source": "KAVACH-07",
            "connectionId": connection_id
        }

        # 5. Sign via eth_account
        signable_msg = encode_typed_data(domain_data=domain, message_types=types, message_data=message)
        signed = wallet.sign_message(signable_msg)
        
        return signed.signature.hex()

import json # Required for the sign_l1_action logic