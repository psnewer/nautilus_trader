"""
赔率订阅消息模型
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class OddsUpdateMessage:
    pair_id: str
    venue: str
    market_type: str
    odds_data: dict[str, Any]


@dataclass
class MatchStatusMessage:
    pair_id: str
    is_live: bool
    source: str = "orbitexch"
