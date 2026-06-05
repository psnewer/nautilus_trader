"""Matching 层:跨 venue 异构 instrument 归一 + 配对(产 MatchedPair + 写 PairRegistry)。"""

from src.arbitrage.matching.actor import MarketMatchingActor
from src.arbitrage.matching.actor import MarketMatchingConfig
from src.arbitrage.matching.engine import MatchEngine
from src.arbitrage.matching.engine import MatchResult
from src.arbitrage.matching.events import MatchedPair
from src.arbitrage.matching.normalizer import NormalizedEvent
from src.arbitrage.matching.normalizer import events_from_instruments
from src.arbitrage.matching.normalizer import normalize_team_name

__all__ = [
    "MarketMatchingActor",
    "MarketMatchingConfig",
    "MatchEngine",
    "MatchResult",
    "MatchedPair",
    "NormalizedEvent",
    "events_from_instruments",
    "normalize_team_name",
]
