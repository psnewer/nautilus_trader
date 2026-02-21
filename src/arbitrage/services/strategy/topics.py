"""
策略服务消息总线主题
"""

OPPORTUNITY_TOPIC_PATTERN = "arbitrage.opportunity.*"


def opportunity_topic(pair_id: str) -> str:
    return f"arbitrage.opportunity.{pair_id}"
