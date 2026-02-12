"""
Way Rebate 信号

基于已有订单计算各平台各结果的当前返水率。

数据来源：
- OrbitExch: WebSocket CURRENT_BETS 消息中的 marketProfit / marketLiability
- Polymarket: 通过 Data API 获取的持仓数据 + User Channel WebSocket 订单更新

计算方式：
- way_rebate = profit / share
- 例：主胜盈利 10 美元，share=100 → way_rebate = 0.10 (10%)

该信号将计算结果写入 context.way_rebate，供 multi-way 信号使用。
"""

from typing import Any, Protocol
from dataclasses import dataclass, field

from .base import Signal, SignalResult, MatchContext


class PositionProtocol(Protocol):
    """持仓数据协议（用于类型检查）"""
    outcome: str
    market_type: str
    size: float
    avg_price: float

    @property
    def profit_if_win(self) -> float: ...

    @property
    def loss_if_lose(self) -> float: ...


@dataclass
class OrbitExchBet:
    """
    OrbitExch 订单数据

    从 WebSocket CURRENT_BETS 消息解析
    """
    offer_id: int
    market_id: str
    selection_id: int
    selection_name: str
    side: str  # "BACK" or "LAY"
    price: float
    size_placed: float
    size_matched: float
    size_remaining: float
    market_profit: float  # 如果该 selection 赢时的盈利
    market_liability: float  # 如果该 selection 输时的亏损
    event_name: str = ""
    competition_name: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "OrbitExchBet":
        """从 CURRENT_BETS 消息中解析"""
        return cls(
            offer_id=data.get("offerId", 0),
            market_id=str(data.get("marketId", "")),
            selection_id=data.get("selectionId", 0),
            selection_name=data.get("selectionName", ""),
            side=data.get("side", ""),
            price=float(data.get("price", 0)),
            size_placed=float(data.get("sizePlaced", 0)),
            size_matched=float(data.get("sizeMatched", 0)),
            size_remaining=float(data.get("sizeRemaining", 0)),
            market_profit=float(data.get("marketProfit", 0)),
            market_liability=float(data.get("marketLiability", 0)),
            event_name=data.get("eventName", ""),
            competition_name=data.get("competitionName", ""),
        )


@dataclass
class MatchPositions:
    """
    单场比赛的持仓汇总

    包含两个平台在各结果上的持仓和盈亏
    """
    pair_id: str
    orbitexch_bets: list[OrbitExchBet] = field(default_factory=list)
    # 使用 Any 以兼容 polymarket_client.PolymarketPosition
    polymarket_positions: list[Any] = field(default_factory=list)

    # 按结果汇总的盈亏（由计算得出）
    # {venue: {outcome: {"profit": x, "liability": y}}}
    pnl_by_outcome: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)

    def calculate_pnl(self, selection_mapping: dict[int, str] = None) -> None:
        """
        计算各结果的盈亏汇总

        Args:
            selection_mapping: OrbitExch selection_id -> outcome 映射
                               例: {123: "home", 456: "away", 789: "draw"}
        """
        self.pnl_by_outcome = {
            "orbitexch": {},
            "polymarket": {},
        }

        # OrbitExch
        for bet in self.orbitexch_bets:
            # 根据 selection_id 确定 outcome
            outcome = "unknown"
            if selection_mapping:
                outcome = selection_mapping.get(bet.selection_id, "unknown")

            if outcome not in self.pnl_by_outcome["orbitexch"]:
                self.pnl_by_outcome["orbitexch"][outcome] = {"profit": 0, "liability": 0}

            # BACK: 如果 selection 赢，获得 profit；如果输，损失 liability
            # LAY: 如果 selection 输，获得 profit；如果赢，损失 liability
            if bet.side == "BACK":
                self.pnl_by_outcome["orbitexch"][outcome]["profit"] += bet.market_profit
                self.pnl_by_outcome["orbitexch"][outcome]["liability"] += bet.market_liability
            else:  # LAY
                # LAY 是反向的
                self.pnl_by_outcome["orbitexch"][outcome]["profit"] -= bet.market_liability
                self.pnl_by_outcome["orbitexch"][outcome]["liability"] -= bet.market_profit

        # Polymarket
        for pos in self.polymarket_positions:
            outcome = pos.outcome
            if outcome not in self.pnl_by_outcome["polymarket"]:
                self.pnl_by_outcome["polymarket"][outcome] = {"profit": 0, "liability": 0}

            self.pnl_by_outcome["polymarket"][outcome]["profit"] += pos.profit_if_win
            self.pnl_by_outcome["polymarket"][outcome]["liability"] += pos.loss_if_lose


class WayRebateSignal(Signal):
    """
    Way Rebate 信号

    基于持仓数据计算各平台各结果的返水率，写入 context.way_rebate。
    """

    @property
    def name(self) -> str:
        return "way-rebate"

    def calculate(self, context: MatchContext, params: dict[str, Any]) -> SignalResult:
        """
        计算 way rebate

        Args:
            context: 比赛上下文
            params: 信号参数
                - share: 计算返水率的基数，默认 100
                - positions: MatchPositions 对象（包含持仓数据）
                - selection_mapping: OrbitExch selection_id -> outcome 映射

        Returns:
            计算结果
        """
        share = params.get("share", 100.0)
        positions: MatchPositions | None = params.get("positions")
        selection_mapping = params.get("selection_mapping", {})

        # 如果没有持仓数据，跳过
        if not positions:
            return SignalResult(
                signal_name=self.name,
                satisfied=True,
                value=None,
                details={"message": "No positions data provided"},
            )

        # 计算盈亏汇总
        positions.calculate_pnl(selection_mapping)

        # 计算 way_rebate 并写入 context
        way_rebate_details = {}

        for venue in ["orbitexch", "polymarket"]:
            venue_pnl = positions.pnl_by_outcome.get(venue, {})
            for outcome, pnl in venue_pnl.items():
                if outcome == "unknown":
                    continue

                # way_rebate = net_profit / share
                # net_profit = profit - liability（如果该 outcome 发生 vs 不发生的净值）
                profit = pnl.get("profit", 0)
                liability = pnl.get("liability", 0)

                # 如果该 outcome 赢：获得 profit
                # 如果该 outcome 输：损失 liability
                # 总期望 = profit - liability (假设 50% 概率)
                # 但这里我们只关心单边，所以用 profit / share
                way_rebate = profit / share if share > 0 else 0

                context.set_way_rebate(venue, outcome, way_rebate)

                if venue not in way_rebate_details:
                    way_rebate_details[venue] = {}
                way_rebate_details[venue][outcome] = {
                    "profit": profit,
                    "liability": liability,
                    "way_rebate": way_rebate,
                }

        return SignalResult(
            signal_name=self.name,
            satisfied=True,
            value=None,
            details={
                "share": share,
                "way_rebate": context.way_rebate,
                "pnl_details": way_rebate_details,
            },
        )
