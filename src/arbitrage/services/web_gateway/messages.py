"""
Web Gateway 管道消息定义
"""

import time
from dataclasses import dataclass, field


@dataclass
class DiscoveryCompleteMessage:
    """Discovery 完成消息"""
    polymarket_count: int
    orbitexch_count: int
    timestamp: float = field(default_factory=time.time)


@dataclass
class MatchingCompleteMessage:
    """Matching 完成消息"""
    pairs_count: int
    timestamp: float = field(default_factory=time.time)
