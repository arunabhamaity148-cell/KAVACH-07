"""
KAVACH-07 — Nuclear-Grade Crypto Futures Signal Bot
Version: v7.0.0
"""

from __future__ import annotations

import logging

# Define version
__version__ = "7.0.0"

# Initialise package-level logger
logger = logging.getLogger("kavach")

# Prevent library from emitting logs if not configured by the application
logger.addHandler(logging.NullHandler())