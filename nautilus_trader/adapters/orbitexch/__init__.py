# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
# -------------------------------------------------------------------------------------------------

"""
OrbitExch adapter for NautilusTrader.

Provides integration with OrbitExch betting exchange using Playwright browser automation.

2026-07-03: NT 适配器层迁移到 `sport/details` API(OrbitExchDiscoveryClient)。
旧 OrbitExchScraper 保留供旧服务层使用,新代码应使用 OrbitExchDiscoveryClient。
"""

__version__ = "0.1.0"

from nautilus_trader.adapters.orbitexch.config import (
    OrbitExchDataClientConfig,
    OrbitExchExecClientConfig,
)
from nautilus_trader.adapters.orbitexch.config_loader import load_config
# New API-based discovery client (2026-07-03)
from nautilus_trader.adapters.orbitexch.discovery_client import (
    OrbitExchDiscoveryClient,
    OrbitExchMarketEvent,
    OrbitExchRunner,
)
# Legacy scraper (kept for old service layer compatibility)
from nautilus_trader.adapters.orbitexch.discovery_scraper import (
    MatchEvent as OrbitExchMatchEvent,
)
from nautilus_trader.adapters.orbitexch.discovery_scraper import OrbitExchScraper
from nautilus_trader.adapters.orbitexch.executor import OrbitExchExecutor
from nautilus_trader.adapters.orbitexch.odds_client import OrbitExchOddsClient


__all__ = [
    # Config
    "OrbitExchDataClientConfig",
    "OrbitExchExecClientConfig",
    "load_config",
    # New discovery client (preferred)
    "OrbitExchDiscoveryClient",
    "OrbitExchMarketEvent",
    "OrbitExchRunner",
    # Legacy scraper (for old service layer)
    "OrbitExchMatchEvent",
    "OrbitExchScraper",
    # Execution
    "OrbitExchExecutor",
    "OrbitExchOddsClient",
]
