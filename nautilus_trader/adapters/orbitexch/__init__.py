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
"""

__version__ = "0.1.0"

from nautilus_trader.adapters.orbitexch.config import (
    OrbitExchDataClientConfig,
    OrbitExchExecClientConfig,
)
from nautilus_trader.adapters.orbitexch.config_loader import (
    create_data_client_config,
    create_exec_client_config,
    load_config,
)


__all__ = [
    "OrbitExchDataClientConfig",
    "OrbitExchExecClientConfig",
    "create_data_client_config",
    "create_exec_client_config",
    "load_config",
]
