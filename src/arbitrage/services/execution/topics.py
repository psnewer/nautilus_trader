"""
执行服务消息总线主题
"""

SESSION_COMPLETE_TOPIC_PATTERN = "arbitrage.session_complete.*"


def session_complete_topic(pair_id: str) -> str:
    return f"arbitrage.session_complete.{pair_id}"
