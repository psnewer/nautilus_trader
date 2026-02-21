"""
执行服务消息模型
"""

from dataclasses import dataclass


@dataclass
class SessionCompleteMessage:
    """执行会话完成消息"""
    session_id: str
    pair_id: str
    opportunity_id: str
    phase: str
    end_reason: str
