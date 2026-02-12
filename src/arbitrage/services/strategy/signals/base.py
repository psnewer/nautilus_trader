"""
信号量基类

定义信号量的抽象接口和结果结构。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ArbitrageVenue(str, Enum):
    """套利平台"""
    POLYMARKET = "polymarket"
    ORBITEXCH = "orbitexch"


class ArbitrageAction(str, Enum):
    """套利动作"""
    BUY = "buy"   # 买入（Polymarket ask / OrbitExch back）
    SELL = "sell"  # 卖出（Polymarket bid / OrbitExch lay）


@dataclass
class ArbitrageLeg:
    """
    套利腿（单个操作）

    Attributes:
        venue: 平台
        market_type: 市场类型 (home/draw/away)
        action: 动作（买入/卖出）
        probability: 概率（0-100）
        raw_odds: 原始赔率
    """
    venue: ArbitrageVenue
    market_type: str
    action: ArbitrageAction
    probability: float
    raw_odds: float


@dataclass
class ArbitrageDirection:
    """
    套利方向

    描述一个完整的套利操作，包含多个腿。

    Attributes:
        direction_id: 方向 ID
        legs: 套利腿列表
        total_probability: 总概率（应 < 100 表示有套利空间）
        rebate_rate: 返水率（正数表示盈利）
        rebate_venue: 返水平台（投注在哪个平台）
        rebate_market: 返水市场类型
        description: 描述
    """
    direction_id: str
    legs: list[ArbitrageLeg]
    total_probability: float
    rebate_rate: float
    rebate_venue: ArbitrageVenue
    rebate_market: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "direction_id": self.direction_id,
            "legs": [
                {
                    "venue": leg.venue.value,
                    "market_type": leg.market_type,
                    "action": leg.action.value,
                    "probability": round(leg.probability, 4),
                    "raw_odds": leg.raw_odds,
                }
                for leg in self.legs
            ],
            "total_probability": round(self.total_probability, 4),
            "rebate_rate": round(self.rebate_rate, 4),
            "rebate_venue": self.rebate_venue.value,
            "rebate_market": self.rebate_market,
            "description": self.description,
        }


@dataclass
class SignalResult:
    """
    信号量计算结果

    Attributes:
        signal_name: 信号名称
        satisfied: 是否满足触发条件
        value: 计算值（可选，如返水率）
        details: 详细信息
    """
    signal_name: str
    satisfied: bool
    value: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "signal_name": self.signal_name,
            "satisfied": self.satisfied,
            "value": self.value,
            "details": self.details,
        }


@dataclass
class MatchContext:
    """
    比赛上下文

    包含信号计算所需的所有信息。

    Attributes:
        pair_id: 比赛 ID
        competition: 联赛名称
        home_team: 主队名称
        away_team: 客队名称
        is_live: 是否为赛中盘
        polymarket_odds: Polymarket 赔率
        orbitexch_odds: OrbitExch 赔率
        arbitrage_directions: 可执行套利方向列表（由信号写入）
        way_rebate: 各平台各结果的当前返水率（基于已有订单计算）
                    格式: {venue: {outcome: rebate_rate}}
                    例: {"polymarket": {"home": 0.05, "away": -0.02}}
    """
    pair_id: str
    competition: str = ""
    home_team: str = ""
    away_team: str = ""
    is_live: bool = False  # 默认赛前盘
    polymarket_odds: dict[str, Any] = field(default_factory=dict)
    orbitexch_odds: dict[str, Any] = field(default_factory=dict)
    arbitrage_directions: list[ArbitrageDirection] = field(default_factory=list)
    way_rebate: dict[str, dict[str, float]] = field(default_factory=dict)

    def clear_directions(self) -> None:
        """清空套利方向列表"""
        self.arbitrage_directions.clear()

    def add_direction(self, direction: ArbitrageDirection) -> None:
        """添加套利方向"""
        self.arbitrage_directions.append(direction)

    def remove_direction(self, direction_id: str) -> bool:
        """移除指定套利方向"""
        for i, d in enumerate(self.arbitrage_directions):
            if d.direction_id == direction_id:
                self.arbitrage_directions.pop(i)
                return True
        return False

    def filter_directions(self, predicate) -> int:
        """
        过滤套利方向，保留满足条件的

        Args:
            predicate: 过滤函数，返回 True 保留，False 移除

        Returns:
            移除的方向数量
        """
        original_count = len(self.arbitrage_directions)
        self.arbitrage_directions = [d for d in self.arbitrage_directions if predicate(d)]
        return original_count - len(self.arbitrage_directions)

    def get_best_direction(self) -> ArbitrageDirection | None:
        """获取最佳套利方向（返水率最高的）"""
        if not self.arbitrage_directions:
            return None
        return max(self.arbitrage_directions, key=lambda d: d.rebate_rate)

    def get_way_rebate(self, venue: str, outcome: str) -> float | None:
        """
        获取指定平台指定结果的当前返水率

        Args:
            venue: 平台 ("polymarket" | "orbitexch")
            outcome: 结果 ("home" | "draw" | "away")

        Returns:
            返水率，如果没有数据返回 None
        """
        venue_data = self.way_rebate.get(venue, {})
        return venue_data.get(outcome)

    def set_way_rebate(self, venue: str, outcome: str, rebate: float) -> None:
        """设置指定平台指定结果的返水率"""
        if venue not in self.way_rebate:
            self.way_rebate[venue] = {}
        self.way_rebate[venue][outcome] = rebate


class Signal(ABC):
    """
    信号量抽象基类

    所有信号量实现需继承此类并实现 calculate 方法。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """信号名称"""
        pass

    @abstractmethod
    def calculate(self, context: MatchContext, params: dict[str, Any]) -> SignalResult:
        """
        计算信号量

        Args:
            context: 比赛上下文
            params: 信号参数（可能被比赛配置覆盖）

        Returns:
            计算结果
        """
        pass

    def _safe_get_odds(
        self,
        odds: dict,
        market_type: str,
        price_type: str,
        default: float = 0.0,
    ) -> float:
        """
        安全获取赔率值

        Args:
            odds: 赔率数据字典
            market_type: 市场类型 (home/draw/away)
            price_type: 价格类型 (bid/ask/back/lay)
            default: 默认值

        Returns:
            赔率值
        """
        try:
            market_data = odds.get(market_type, {})
            if isinstance(market_data, dict):
                return float(market_data.get(price_type, default))
            return default
        except (TypeError, ValueError):
            return default
