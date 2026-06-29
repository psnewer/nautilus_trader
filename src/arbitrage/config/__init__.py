"""
Arbitrage 配置面(Q22-Q27 锁定 / #41):msgspec schema + JSON loader + env 凭证注入 + dispatcher。

详设见 `docs/arbitrage/architectures/_cross-cutting/configuration.md`。

入口:`load_arb_config(path)` → `ArbConfig`。
"""

from src.arbitrage.config.schema import ArbConfig
from src.arbitrage.config.schema import ArbitrageSectionConfig
from src.arbitrage.config.schema import ConfigError
from src.arbitrage.config.schema import DebugSectionConfig
from src.arbitrage.config.schema import DiscoveryConfig
from src.arbitrage.config.schema import ExecutionSectionConfig
from src.arbitrage.config.schema import MatchingConfig
from src.arbitrage.config.schema import OrbitExchSectionConfig
from src.arbitrage.config.schema import PolymarketSectionConfig
from src.arbitrage.config.schema import RiskSectionConfig
from src.arbitrage.config.schema import SportFilter
from src.arbitrage.config.schema import StrategyBindingConfig
from src.arbitrage.config.schema import StrategySectionConfig
from src.arbitrage.config.schema import VenueDiscoveryConfig
from src.arbitrage.config.schema import VenuesConfig

__all__ = [
    "ArbConfig",
    "ArbitrageSectionConfig",
    "ConfigError",
    "DebugSectionConfig",
    "DiscoveryConfig",
    "ExecutionSectionConfig",
    "MatchingConfig",
    "OrbitExchSectionConfig",
    "PolymarketSectionConfig",
    "RiskSectionConfig",
    "SportFilter",
    "StrategyBindingConfig",
    "StrategySectionConfig",
    "VenueDiscoveryConfig",
    "VenuesConfig",
    "load_arb_config",
]


def load_arb_config(path):
    """避免循环 import,延迟解析。"""
    from src.arbitrage.config.loader import load_arb_config as _impl
    return _impl(path)
