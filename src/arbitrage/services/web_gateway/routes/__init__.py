"""
Web Gateway 路由模块
"""

from .discovery import router as discovery_router
from .matching import router as matching_router
from .config import router as config_router
from .odds import router as odds_router
from .strategy import router as strategy_router
from .execution import router as execution_router
from .debug import router as debug_router
from .risk import router as risk_router

__all__ = [
    "discovery_router",
    "matching_router",
    "config_router",
    "odds_router",
    "strategy_router",
    "execution_router",
    "debug_router",
    "risk_router",
]
