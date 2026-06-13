"""
KAVACH-07 — Strategies Registry
Exports all strategy modules and providing a factory for symbol-specific initialization.
"""

from __future__ import annotations
from typing import Any, List, Dict, Type

from kavach.strategies.base import StrategyBase
from kavach.strategies.regime_filter import RegimeFilter
from kavach.strategies.oi_breakout import OiBreakout
from kavach.strategies.funding_squeeze import FundingSqueeze
from kavach.strategies.hyperliquid_leadlag import HyperliquidLeadlag
from kavach.strategies.etf_flow import EtfFlow
from kavach.strategies.stablecoin_flow import StablecoinFlow
from kavach.strategies.onchain_liq import OnchainLiq
from kavach.strategies.sector_rotation import SectorRotation
from kavach.strategies.cvd_divergence import CvdDivergence
from kavach.strategies.absorption_detection import AbsorptionDetection
from kavach.strategies.vwap_reversion import VwapReversion
from kavach.strategies.spot_accumulation import SpotAccumulation
from kavach.strategies.liquidation_fade import LiquidationFade
from kavach.strategies.social_fade import SocialFade
from kavach.strategies.dex_cex_arb import DexCexArb
from kavach.strategies.post_settlement import PostSettlement
from kavach.strategies.tokenized_security import TokenizedSecurity
from kavach.strategies.liquidation_cascade import LiquidationCascade
from kavach.strategies.market_maker_pnl import MarketMakerPnl
from kavach.strategies.lead_lag import LeadLag
from kavach.strategies.liquidity_flow import LiquidityFlow

# Central registry of all strategy classes
ALL_STRATEGY_CLASSES: List[Type[StrategyBase]] = [
    RegimeFilter,
    OiBreakout,
    FundingSqueeze,
    HyperliquidLeadlag,
    EtfFlow,
    StablecoinFlow,
    OnchainLiq,
    SectorRotation,
    CvdDivergence,
    AbsorptionDetection,
    VwapReversion,
    SpotAccumulation,
    LiquidationFade,
    SocialFade,
    DexCexArb,
    PostSettlement,
    TokenizedSecurity,
    LiquidationCascade,
    MarketMakerPnl,
    LeadLag,
    LiquidityFlow,
]

def build_strategies_for_symbol(config: Dict[str, Any], symbol: str) -> List[StrategyBase]:
    """
    Instantiates all enabled strategies for a specific symbol based on config.yaml.
    Searches both 'strategies' and 'phase2_strategies' blocks.
    """
    instances: List[StrategyBase] = []
    
    for cls in ALL_STRATEGY_CLASSES:
        # Determine the config key for the class (snake_case)
        # We use the instance's _config_key() logic statically here
        name = cls.__name__
        res = [name[0].lower()]
        for ch in name[1:]:
            if ch.isupper():
                res.append("_")
                res.append(ch.lower())
            else:
                res.append(ch)
        key = "".join(res)
        
        # Check standard strategies block
        strat_cfg = config.get("strategies", {}).get(key)
        # Check phase2 strategies block
        if not strat_cfg:
            strat_cfg = config.get("phase2_strategies", {}).get(key)
            
        if strat_cfg and strat_cfg.get("enabled", False):
            instances.append(cls(config, symbol))
            
    return instances