"""
风控服务消息总线主题
"""

WAY_REBATE_TOPIC_PATTERN = "arbitrage.way_rebate.*"


def way_rebate_topic(pair_id: str) -> str:
    return f"arbitrage.way_rebate.{pair_id}"
