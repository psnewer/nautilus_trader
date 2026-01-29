"""
市场匹配服务

负责将不同平台发现的比赛事件进行匹配，识别相同的比赛。
"""

from .config import MarketMatchingConfig
from .engine import MatchEngine
from .normalizer import EventNormalizer, NormalizedEvent
from .service import MarketMatchingService, MatchedPair

__all__ = [
    "MarketMatchingConfig",
    "MatchEngine",
    "EventNormalizer",
    "NormalizedEvent",
    "MarketMatchingService",
    "MatchedPair",
]
