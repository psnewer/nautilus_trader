"""
风控服务消息模型
"""

from dataclasses import dataclass


@dataclass
class WayRebateMessage:
    """持仓返水率消息"""
    pair_id: str
    way_rebate: dict[str, float]                       # {outcome: rebate_rate}
    way_rebate_by_venue: dict[str, dict[str, float]]   # {venue: {outcome: rebate_rate}}
    min_way_rebate: float | None
