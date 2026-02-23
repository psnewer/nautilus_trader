"""
策略服务消息模型
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OpportunityMessage:
    """套利机会消息"""
    opportunity_id: str
    pair_id: str
    competition: str
    home_team: str
    away_team: str
    is_live: bool
    detected_at: float
    triggered_strategies: list[str]
    rebate_value: float | None
    way_rebate: dict[str, float]
    best_direction: dict[str, Any] | None
    all_directions: list[dict[str, Any]]
    signals: dict[str, dict[str, Any]]
    adjusted_share: float | None = None
    status: str = "detected"
