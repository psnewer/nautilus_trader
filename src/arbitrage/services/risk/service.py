"""
风控服务

负责止损检查和持仓风险管理。
"""

import logging
from dataclasses import dataclass
from typing import Any

from .config import RiskConfig
from .position import PositionManager, MatchPosition


@dataclass
class RiskCheckResult:
    """
    风控检查结果

    Attributes:
        allowed: 是否允许下注
        reason: 拒绝原因（如果不允许）
        match_blocked: 是否因单场止损被阻止
        global_blocked: 是否因全局止损被阻止
        tp_blocked: 是否因止盈被阻止
        way_rebate: 该比赛的各方向返水率
        min_way_rebate: 该比赛的最低返水率
        global_min_sum: 全局最低返水率之和
    """
    allowed: bool
    reason: str = ""
    match_blocked: bool = False
    global_blocked: bool = False
    tp_blocked: bool = False
    way_rebate: dict[str, float] | None = None
    min_way_rebate: float | None = None
    global_min_sum: float | None = None

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "match_blocked": self.match_blocked,
            "global_blocked": self.global_blocked,
            "tp_blocked": self.tp_blocked,
            "way_rebate": self.way_rebate,
            "min_way_rebate": self.min_way_rebate,
            "global_min_sum": self.global_min_sum,
        }


class RiskService:
    """
    风控服务

    功能：
    1. 跟踪各比赛的持仓
    2. 计算各方向的持仓返水率
    3. 提供止损检查
    4. 阻止触发止损的比赛继续下注
    """

    def __init__(
        self,
        config: RiskConfig | None = None,
        logger: logging.Logger | None = None,
        share: float = 100.0,
    ):
        self._config = config or RiskConfig()
        self._log = logger or logging.getLogger(self.__class__.__name__)
        self._position_manager = PositionManager(default_share=share)

    @property
    def config(self) -> RiskConfig:
        return self._config

    def update_config(self, config: RiskConfig) -> None:
        """更新配置"""
        self._config = config
        self._log.info(f"Risk config updated: match_sl={config.match_sl}, global_sl={config.global_sl}")

    def set_share(self, share: float) -> None:
        """设置 share 参数"""
        self._position_manager.set_share(share)
        self._log.info(f"Share updated: {share}")

    # =========================================================================
    # 历史持仓加载
    # =========================================================================

    def load_historical_positions(
        self,
        polymarket_positions: list,
        orbitexch_bets: list[dict],
        polymarket_pair_mapping: dict[str, str],
        orbitexch_pair_mapping: dict[str, str],
        selection_mappings: dict[str, dict[int, str]],
    ) -> dict[str, int]:
        """
        加载历史持仓数据

        在服务启动时调用，从 API 获取的历史持仓数据加载到 PositionManager。

        Args:
            polymarket_positions: PolymarketPosition 列表
            orbitexch_bets: OrbitExch bet 列表（dict 格式）
            polymarket_pair_mapping: event_id -> pair_id 映射
            orbitexch_pair_mapping: market_id -> pair_id 映射
            selection_mappings: pair_id -> {selection_id: market_type} 映射

        Returns:
            {"polymarket": count, "orbitexch": count}
        """
        # 清空现有持仓（重新加载）
        self._position_manager.clear()

        # 加载 Polymarket 持仓
        pm_count = self._position_manager.load_polymarket_positions(
            positions=polymarket_positions,
            pair_mapping=polymarket_pair_mapping,
        )

        # 加载 OrbitExch 持仓
        oe_count = self._position_manager.load_orbitexch_bets(
            bets=orbitexch_bets,
            pair_mapping=orbitexch_pair_mapping,
            selection_mappings=selection_mappings,
        )

        self._log.info(
            f"Loaded historical positions: Polymarket={pm_count}, OrbitExch={oe_count}"
        )

        # 记录各比赛的 way_rebate
        for position in self._position_manager.get_all_positions():
            way_rebate = position.calculate_way_rebate()
            if way_rebate:
                self._log.debug(
                    f"Position {position.pair_id}: way_rebate={way_rebate}"
                )

        return {"polymarket": pm_count, "orbitexch": oe_count}

    # =========================================================================
    # 持仓管理
    # =========================================================================

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
        记录订单成交（仅记录，不触发返水率计算）

        在套利机会执行过程中，每笔订单成交时调用。
        不立即计算 way_rebate，避免一方先成交、对手盘未成交时触发假止损。

        通过 order_id 去重：同一订单多次调用时更新 size 而非新增 leg。

        Args:
            pair_id: 比赛 ID
            venue: 平台 (polymarket/orbitexch)
            market_type: 方向 (home/draw/away)
            size: 成交数量
            price: 成交价格
            order_id: 订单 ID（用于去重）
            competition: 联赛名称
            home_team: 主队
            away_team: 客队
        """
        self._position_manager.add_fill(
            pair_id=pair_id,
            venue=venue,
            market_type=market_type,
            size=size,
            price=price,
            order_id=order_id,
            competition=competition,
            home_team=home_team,
            away_team=away_team,
        )

        self._log.debug(
            f"Fill recorded: {pair_id} {venue}/{market_type}, "
            f"order_id={order_id}, size={size}, price={price}"
        )

    def on_execution_complete(self, pair_id: str) -> None:
        """
        套利机会执行完成回调

        在一次套利机会的所有订单执行完成后调用。
        此时才重新计算 way_rebate，避免单腿成交期间的假止损。

        Args:
            pair_id: 比赛 ID
        """
        way_rebate = self._position_manager.get_way_rebate(pair_id)
        min_rebate = min(way_rebate.values()) if way_rebate else None

        self._log.info(
            f"Execution complete, position recalculated: {pair_id}, "
            f"way_rebate={way_rebate}, min={min_rebate}"
        )

    def close_match(self, pair_id: str) -> None:
        """
        标记比赛已结束

        已结束的比赛不参与全局止损计算。

        Args:
            pair_id: 比赛 ID
        """
        self._position_manager.close_match(pair_id)
        self._log.info(f"Match closed: {pair_id}")

    # =========================================================================
    # 风控检查
    # =========================================================================

    def check_risk(self, pair_id: str) -> RiskCheckResult:
        """
        检查是否允许下注

        检查顺序：
        1. 风控是否启用
        2. 单场止盈检查（所有方向返水率 >= tp）
        3. 单场止损检查（最小方向返水率 < sl）
        4. 全局累计止损检查

        Args:
            pair_id: 比赛 ID

        Returns:
            检查结果
        """
        # 风控未启用
        if not self._config.enabled:
            return RiskCheckResult(allowed=True, reason="Risk disabled")

        # 获取持仓数据
        position = self._position_manager.get_position(pair_id)
        way_rebate = position.calculate_way_rebate() if position else {}
        min_way_rebate = min(way_rebate.values()) if way_rebate else None
        global_min_sum = self._position_manager.get_global_min_rebate_sum()

        # 1. 单场止盈检查：所有方向返水率 >= tp
        if way_rebate:
            match_tp = self._config.match_tp
            all_above_tp = all(rebate >= match_tp for rebate in way_rebate.values())
            if all_above_tp:
                self._log.info(
                    f"Match take profit triggered: {pair_id}, "
                    f"all way_rebate >= tp={match_tp:.2%}, way_rebate={way_rebate}"
                )
                return RiskCheckResult(
                    allowed=False,
                    reason=f"Match take profit: all way_rebate >= {match_tp:.2%}",
                    tp_blocked=True,
                    way_rebate=way_rebate,
                    min_way_rebate=min_way_rebate,
                    global_min_sum=global_min_sum,
                )

        # 2. 单场止损检查
        if min_way_rebate is not None:
            match_sl = self._config.get_match_sl(pair_id)
            if min_way_rebate < match_sl:
                self._log.warning(
                    f"Match stop loss triggered: {pair_id}, "
                    f"min_way_rebate={min_way_rebate:.4f} < sl={match_sl}"
                )
                return RiskCheckResult(
                    allowed=False,
                    reason=f"Match stop loss: min_way_rebate={min_way_rebate:.2%} < {match_sl:.2%}",
                    match_blocked=True,
                    way_rebate=way_rebate,
                    min_way_rebate=min_way_rebate,
                    global_min_sum=global_min_sum,
                )

        # 3. 全局累计止损检查
        if global_min_sum < self._config.global_sl:
            self._log.warning(
                f"Global stop loss triggered: "
                f"global_min_sum={global_min_sum:.4f} < sl={self._config.global_sl}"
            )
            return RiskCheckResult(
                allowed=False,
                reason=f"Global stop loss: sum={global_min_sum:.2%} < {self._config.global_sl:.2%}",
                global_blocked=True,
                way_rebate=way_rebate,
                min_way_rebate=min_way_rebate,
                global_min_sum=global_min_sum,
            )

        # 通过检查
        return RiskCheckResult(
            allowed=True,
            way_rebate=way_rebate,
            min_way_rebate=min_way_rebate,
            global_min_sum=global_min_sum,
        )

    def is_match_allowed(self, pair_id: str) -> bool:
        """
        快速检查是否允许下注

        Args:
            pair_id: 比赛 ID

        Returns:
            是否允许
        """
        return self.check_risk(pair_id).allowed

    # =========================================================================
    # 查询
    # =========================================================================

    def get_position(self, pair_id: str) -> MatchPosition | None:
        """获取比赛持仓"""
        return self._position_manager.get_position(pair_id)

    def get_way_rebate(self, pair_id: str) -> dict[str, float]:
        """获取比赛各方向返水率"""
        return self._position_manager.get_way_rebate(pair_id)

    def get_all_way_rebates(self) -> dict[str, dict[str, float]]:
        """获取所有比赛的返水率"""
        result = {}
        for position in self._position_manager.get_all_positions():
            result[position.pair_id] = position.calculate_way_rebate()
        return result

    def get_global_status(self) -> dict[str, Any]:
        """
        获取全局风控状态

        Returns:
            {
                "enabled": bool,
                "match_sl": float,
                "global_sl": float,
                "match_tp": float,
                "global_min_sum": float,
                "global_blocked": bool,
                "active_positions": int,
                "blocked_matches": list[str],
                "tp_matches": list[str],
            }
        """
        global_min_sum = self._position_manager.get_global_min_rebate_sum()
        global_blocked = global_min_sum < self._config.global_sl if self._config.enabled else False

        # 检查每场比赛是否被阻止（止损或止盈）
        blocked_matches = []
        tp_matches = []
        for position in self._position_manager.get_active_positions():
            way_rebate = position.calculate_way_rebate()
            min_rebate = position.get_min_way_rebate()

            # 检查止盈：所有方向返水率 >= tp
            if way_rebate:
                all_above_tp = all(rebate >= self._config.match_tp for rebate in way_rebate.values())
                if all_above_tp:
                    tp_matches.append(position.pair_id)
                    continue  # 止盈的不再检查止损

            # 检查止损
            if min_rebate is not None:
                match_sl = self._config.get_match_sl(position.pair_id)
                if min_rebate < match_sl:
                    blocked_matches.append(position.pair_id)

        return {
            "enabled": self._config.enabled,
            "match_sl": self._config.match_sl,
            "global_sl": self._config.global_sl,
            "match_tp": self._config.match_tp,
            "global_min_sum": global_min_sum,
            "global_blocked": global_blocked,
            "active_positions": len(self._position_manager.get_active_positions()),
            "total_positions": len(self._position_manager.get_all_positions()),
            "blocked_matches": blocked_matches,
            "tp_matches": tp_matches,
        }

    def get_positions_summary(self) -> dict[str, Any]:
        """获取持仓摘要"""
        return self._position_manager.to_dict()

    def clear_positions(self) -> None:
        """清空所有持仓（谨慎使用）"""
        self._position_manager.clear()
        self._log.warning("All positions cleared")
