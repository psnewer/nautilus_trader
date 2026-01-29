"""
Web Gateway 路由模块
"""

from .discovery import router as discovery_router
from .matching import router as matching_router
from .config import router as config_router
from .odds import router as odds_router

__all__ = [
    "discovery_router",
    "matching_router",
    "config_router",
    "odds_router",
]
