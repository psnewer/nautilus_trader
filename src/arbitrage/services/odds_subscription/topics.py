"""
赔率订阅消息总线主题
"""


ODDS_TOPIC_PATTERN = "arbitrage.odds.*"
MATCH_STATUS_TOPIC_PATTERN = "arbitrage.match_status.*"
PAIR_ACTIVITY_TOPIC_PATTERN = "arbitrage.pair_activity.*"


def odds_topic(venue: str, pair_id: str, market_type: str) -> str:
    return f"arbitrage.odds.{venue}.{pair_id}.{market_type}"


def match_status_topic(pair_id: str) -> str:
    return f"arbitrage.match_status.{pair_id}"


def pair_activity_topic(pair_id: str) -> str:
    return f"arbitrage.pair_activity.{pair_id}"
