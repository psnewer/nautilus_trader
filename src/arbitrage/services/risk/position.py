"""
持仓管理和返水率计算

持仓返水率 (way_rebate) 计算逻辑：
- 对于每个方向（home/draw/away），计算如果该方向获胜时的盈亏
- way_rebate[outcome] = (该方向获胜时的净收益) / share

计算公式：
假设 share = 100，在某场比赛中：
- Polymarket home: 买入 100 份，价格 0.4，成本 40，获胜收益 100
- OrbitExch away: 下注 50，赔率 2.5，获胜收益 125

如果 home 获胜：
  - Polymarket home 收益: 100 - 40 = 60
  - OrbitExch away 损失: -50
  - 净收益: 60 - 50 = 10
  - way_rebate[home] = 10 / 100 = 0.10 (10%)

如果 away 获胜：
  - Polymarket home 损失: -40
  - OrbitExch away 收益: 125 - 50 = 75
  - 净收益: -40 + 75 = 35
  - way_rebate[away] = 35 / 100 = 0.35 (35%)
"""

from dataclasses import dataclass, field
import logging
from typing import Any
import time


logger = logging.getLogger("PositionManager")


@dataclass
class PositionLeg:
    """
    单腿持仓

    Attributes:
        venue: 平台 (polymarket/orbitexch)
        market_type: 市场类型 (home/draw/away)
        size: 下注金额（OrbitExch）或购买份数（Polymarket）
        price: 价格（Polymarket 0-1 概率）或赔率（OrbitExch 十进制赔率）
        order_id: 订单 ID（用于去重，同一订单更新而非新增）
        filled_at: 成交时间
        profit_override: 直接设置的盈利值（用于从 API 加载，跳过计算）
        loss_override: 直接设置的亏损值（用于从 API 加载，跳过计算）
    """
    venue: str
    market_type: str
    size: float
    price: float
    order_id: str = ""
    filled_at: float = field(default_factory=time.time)
    profit_override: float | None = None
    loss_override: float | None = None
    fx: float = 1.0  # 汇率（用于 OrbitExch 货币转换，默认 1.0 不转换）

    def profit_if_wins(self) -> float:
        """
        如果该方向获胜时的收益

        如果设置了 profit_override，直接返回该值。
        否则根据 venue 类型计算：
        - Polymarket: size * (1 - price)
        - OrbitExch: size * (price - 1) * fx
        """
        if self.profit_override is not None:
            return self.profit_override

        if self.venue == "polymarket":
            return self.size * (1 - self.price)
        else:  # orbitexch
            return self.size * (self.price - 1) * self.fx

    def loss_if_loses(self) -> float:
        """
        如果该方向输掉时的损失（返回正数）

        如果设置了 loss_override，直接返回该值。
        否则根据 venue 类型计算：
        - Polymarket: size * price
        - OrbitExch: size * fx
        """
        if self.loss_override is not None:
            return self.loss_override

        if self.venue == "polymarket":
            return self.size * self.price
        else:  # orbitexch
            return self.size * self.fx

    def to_dict(self) -> dict:
        return {
            "venue": self.venue,
            "market_type": self.market_type,
            "size": self.size,
            "price": self.price,
            "order_id": self.order_id,
            "filled_at": self.filled_at,
            "profit_if_wins": self.profit_if_wins(),
            "loss_if_loses": self.loss_if_loses(),
        }


@dataclass
class MatchPosition:
    """
    单场比赛的持仓

    Attributes:
        pair_id: 比赛 ID
        competition: 联赛名称
        home_team: 主队
        away_team: 客队
        legs: 持仓腿列表
        closed: 比赛是否已结束
        share: 基准金额（用于计算返水率）
    """
    pair_id: str
    competition: str = ""
    home_team: str = ""
    away_team: str = ""
    legs: list[PositionLeg] = field(default_factory=list)
    closed: bool = False
    share: float = 100.0

    def add_leg(self, leg: PositionLeg) -> None:
        """
        添加持仓腿

        如果 order_id 非空且已存在同 order_id 的腿，更新该腿的 size
        （处理部分成交后追加成交的场景），不新增。
        """
        if leg.order_id:
            for existing in self.legs:
                if existing.order_id == leg.order_id:
                    existing.size = leg.size
                    existing.filled_at = leg.filled_at
                    return
        self.legs.append(leg)

    def get_outcomes(self) -> set[str]:
        """获取所有涉及的方向"""
        outcomes = set()
        for leg in self.legs:
            outcomes.add(leg.market_type)
        # 标准化：如果有 home 和 away，检查是否需要 draw
        if "home" in outcomes and "away" in outcomes:
            # 3-way 市场可能有 draw
            pass
        return outcomes

    def calculate_way_rebate(self) -> dict[str, float]:
        """
        计算各方向的持仓返水率

        Returns:
            {outcome: rebate_rate}
        """
        if not self.legs:
            return {}

        # 获取所有方向
        all_outcomes = {"home", "away"}
        # 检查是否有 draw 持仓
        for leg in self.legs:
            if leg.market_type == "draw":
                all_outcomes.add("draw")
                break

        way_rebate = {}

        for outcome in all_outcomes:
            # 计算如果这个方向获胜时的净收益
            total_profit = 0.0

            for leg in self.legs:
                if leg.market_type == outcome:
                    # 这个腿获胜
                    total_profit += leg.profit_if_wins()
                else:
                    # 这个腿输掉
                    total_profit -= leg.loss_if_loses()

            # 返水率 = 净收益 / share
            if self.share > 0:
                way_rebate[outcome] = total_profit / self.share
            else:
                way_rebate[outcome] = 0.0

        return way_rebate

    def calculate_way_rebate_by_venue(self) -> dict[str, dict[str, float]]:
        """
        按平台计算各方向的持仓返水率

        Returns:
            {venue: {outcome: rebate_rate}}
        """
        if not self.legs:
            return {}

        venues = {leg.venue for leg in self.legs}
        result: dict[str, dict[str, float]] = {}
        for venue in venues:
            venue_legs = [leg for leg in self.legs if leg.venue == venue]
            if not venue_legs:
                continue
            temp = MatchPosition(
                pair_id=self.pair_id,
                competition=self.competition,
                home_team=self.home_team,
                away_team=self.away_team,
                legs=venue_legs,
                closed=self.closed,
                share=self.share,
            )
            result[venue] = temp.calculate_way_rebate()
        return result

    def get_min_way_rebate(self) -> float | None:
        """
        获取最低方向的持仓返水率

        Returns:
            最低返水率，如果没有持仓返回 None
        """
        way_rebate = self.calculate_way_rebate()
        if not way_rebate:
            return None
        return min(way_rebate.values())

    def get_total_cost(self) -> float:
        """获取总成本"""
        total = 0.0
        for leg in self.legs:
            total += leg.loss_if_loses()
        return total

    def to_dict(self) -> dict:
        way_rebate = self.calculate_way_rebate()
        return {
            "pair_id": self.pair_id,
            "competition": self.competition,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "legs": [leg.to_dict() for leg in self.legs],
            "closed": self.closed,
            "share": self.share,
            "way_rebate": way_rebate,
            "way_rebate_by_venue": self.calculate_way_rebate_by_venue(),
            "min_way_rebate": min(way_rebate.values()) if way_rebate else None,
            "total_cost": self.get_total_cost(),
        }


class PositionManager:
    """
    持仓管理器

    跟踪所有比赛的持仓，计算返水率。
    """

    def __init__(self, default_share: float = 100.0):
        self._positions: dict[str, MatchPosition] = {}  # pair_id -> MatchPosition
        self._default_share = default_share
        self._fx = 1.0

    def get_or_create_position(
        self,
        pair_id: str,
        competition: str = "",
        home_team: str = "",
        away_team: str = "",
    ) -> MatchPosition:
        """获取或创建比赛持仓"""
        if pair_id not in self._positions:
            self._positions[pair_id] = MatchPosition(
                pair_id=pair_id,
                competition=competition,
                home_team=home_team,
                away_team=away_team,
                share=self._default_share,
            )
        return self._positions[pair_id]

    def add_fill(
        self,
        pair_id: str,
        venue: str,
        market_type: str,
        size: float,
        price: float,
        order_id: str = "",
        competition: str = "",
        home_team: str = "",
        away_team: str = "",
    ) -> None:
        """
        添加成交记录

        如果 order_id 非空且已存在，更新 size（去重）。

        Args:
            pair_id: 比赛 ID
            venue: 平台
            market_type: 方向
            size: 下注金额/份数
            price: 价格/赔率
            order_id: 订单 ID（用于去重）
            competition: 联赛（可选）
            home_team: 主队（可选）
            away_team: 客队（可选）
        """
        position = self.get_or_create_position(
            pair_id, competition, home_team, away_team
        )
        position.add_leg(PositionLeg(
            venue=venue,
            market_type=market_type,
            size=size,
            price=price,
            order_id=order_id,
            fx=self._fx if venue == "orbitexch" else 1.0,
        ))

    def refresh_position(self, pair_id: str) -> None:
        """
        清除指定 pair 的持仓腿，保留 pair 元信息（competition/teams 等）

        用于从 API 数据重建该 pair 的持仓。

        Args:
            pair_id: 比赛 ID
        """
        position = self._positions.get(pair_id)
        if position:
            position.legs.clear()

    def close_match(self, pair_id: str) -> None:
        """标记比赛已结束"""
        if pair_id in self._positions:
            self._positions[pair_id].closed = True

    def get_position(self, pair_id: str) -> MatchPosition | None:
        """获取比赛持仓"""
        return self._positions.get(pair_id)

    def get_active_positions(self) -> list[MatchPosition]:
        """获取所有未结束比赛的持仓"""
        return [p for p in self._positions.values() if not p.closed]

    def get_all_positions(self) -> list[MatchPosition]:
        """获取所有持仓"""
        return list(self._positions.values())

    def get_way_rebate(self, pair_id: str) -> dict[str, float]:
        """获取指定比赛的各方向返水率"""
        position = self._positions.get(pair_id)
        if not position:
            return {}
        return position.calculate_way_rebate()

    def get_min_way_rebate(self, pair_id: str) -> float | None:
        """获取指定比赛的最低方向返水率"""
        position = self._positions.get(pair_id)
        if not position:
            return None
        return position.get_min_way_rebate()

    def get_global_min_rebate_sum(self) -> float:
        """
        获取所有比赛的最低方向返水率之和

        用于全局累计止损判断。
        包括已完成和未完成的比赛，只要有持仓数据就计入。
        服务重启后，已结算比赛的仓位数据消失，自然不再计入。
        """
        total = 0.0
        for position in self._positions.values():
            min_rebate = position.get_min_way_rebate()
            if min_rebate is not None:
                total += min_rebate
        return total

    def set_share(self, share: float) -> None:
        """设置默认 share 并更新所有持仓"""
        self._default_share = share
        for position in self._positions.values():
            position.share = share

    def set_fx(self, fx: float) -> None:
        """设置汇率并更新所有 OrbitExch 持仓的 fx"""
        self._fx = fx
        for position in self._positions.values():
            for leg in position.legs:
                if leg.venue == "orbitexch":
                    leg.fx = fx

    def clear(self) -> None:
        """清空所有持仓"""
        self._positions.clear()

    # =========================================================================
    # 从 API 加载历史持仓
    # =========================================================================

    def load_polymarket_positions(
        self,
        positions: list,
        pair_mapping: dict[str, str],
    ) -> int:
        """
        从 Polymarket 持仓数据加载

        Args:
            positions: PolymarketPosition 列表
            pair_mapping: event_id -> pair_id 映射

        Returns:
            加载的持仓数量
        """
        count = 0
        for pos in positions:
            # 获取 pair_id
            event_id = getattr(pos, "event_id", "")
            pair_id = pair_mapping.get(event_id)
            if not pair_id:
                continue

            # 获取 market_type
            market_type = getattr(pos, "market_type", "")
            if not market_type:
                continue

            # 获取数据
            size = getattr(pos, "size", 0)
            avg_price = getattr(pos, "avg_price", 0)

            if size <= 0:
                continue

            # 添加持仓
            position = self.get_or_create_position(pair_id)
            leg = PositionLeg(
                venue="polymarket",
                market_type=market_type,
                size=size,
                price=avg_price,
            )
            position.add_leg(leg)
            logger.debug(
                "Risk load PM leg: "
                f"pair_id={pair_id}, event_id={event_id}, market_type={market_type}, "
                f"size={leg.size:.4f}, price={leg.price:.6f}, "
                f"profit_if_wins={leg.profit_if_wins():.6f}, "
                f"loss_if_loses={leg.loss_if_loses():.6f}"
            )
            count += 1

        return count

    def load_orbitexch_bets(
        self,
        bets: list[dict],
        pair_mapping: dict[str, str],
        selection_mappings: dict[str, dict[int, str]],
    ) -> int:
        """
        从 OrbitExch 订单数据加载

        Args:
            bets: OrbitExch bet 列表（dict 格式）
            pair_mapping: market_id -> pair_id 映射
            selection_mappings: pair_id -> {selection_id: market_type} 映射

        Returns:
            加载的持仓数量
        """
        count = 0
        for bet in bets:
            # 获取 pair_id
            market_id = str(bet.get("marketId", ""))
            pair_id = pair_mapping.get(market_id)
            if not pair_id:
                continue

            # 获取 market_type (selection -> outcome)
            selection_id = bet.get("selectionId", 0)
            selection_mapping = selection_mappings.get(pair_id, {})
            market_type = selection_mapping.get(selection_id, "")
            if not market_type:
                continue

            # 只处理已成交的订单
            size_matched = float(bet.get("sizeMatched", 0))
            if size_matched <= 0:
                continue

            average_price_raw = bet.get("averagePrice", 0)
            try:
                avg_price = float(average_price_raw)
            except (TypeError, ValueError):
                avg_price = 0.0

            if avg_price <= 0:
                logger.warning(
                    "OrbitExch bet missing averagePrice, skip loading: "
                    f"marketId={market_id}, selectionId={selection_id}, sizeMatched={size_matched}"
                )
                continue

            side = bet.get("side", "BACK")
            profit_net = float(bet.get("profitNet", 0))
            liability = float(bet.get("liability", 0))

            # 添加持仓
            position = self.get_or_create_position(pair_id)
            leg = PositionLeg(
                venue="orbitexch",
                market_type=market_type,
                size=size_matched,
                price=avg_price,
                fx=self._fx,
            )
            position.add_leg(leg)
            logger.debug(
                "Risk load OE leg: "
                f"pair_id={pair_id}, market_id={market_id}, selection_id={selection_id}, "
                f"market_type={market_type}, side={side}, size={leg.size:.4f}, "
                f"average_price={leg.price:.6f}, fx={leg.fx:.4f}, "
                f"profit_net={profit_net}, liability={liability}, "
                f"profit_override={leg.profit_override}, loss_override={leg.loss_override}"
            )
            count += 1

        return count

    def to_dict(self) -> dict[str, Any]:
        """导出所有持仓数据"""
        return {
            "default_share": self._default_share,
            "positions": {
                pid: pos.to_dict()
                for pid, pos in self._positions.items()
            },
            "global_min_rebate_sum": self.get_global_min_rebate_sum(),
            "active_count": len(self.get_active_positions()),
            "total_count": len(self._positions),
        }
