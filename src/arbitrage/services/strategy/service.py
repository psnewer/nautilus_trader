"""
套利策略服务

负责信号量计算、策略评估和机会检测。
支持插拔式的 Signal 和 Strategy 配置。
"""

import logging
import time
from typing import Any, Callable

from .config import StrategyServiceConfig, MatchConfig
from .signals import get_signal, SignalResult, MatchContext
from .strategies import Strategy, StrategyResult, DefaultStrategy, get_strategy_class


class StrategyService:
    """
    策略服务

    功能：
    1. 接收赔率更新并计算信号量
    2. 根据配置评估策略
    3. 支持联赛/比赛级别的策略配置
    4. 检测套利机会
    """

    def __init__(
        self,
        config: StrategyServiceConfig | None = None,
        logger: logging.Logger | None = None,
    ):
        self._config = config or StrategyServiceConfig()
        self._log = logger or logging.getLogger(self.__class__.__name__)

        # 比赛信息缓存：pair_id -> MatchContext
        self._match_contexts: dict[str, MatchContext] = {}

        # 赔率缓存：pair_id -> {venue -> {market_type -> odds_data}}
        self._odds_cache: dict[str, dict[str, dict[str, Any]]] = {}

        # 信号量结果缓存：pair_id -> {signal_name -> SignalResult}
        self._signal_results: dict[str, dict[str, SignalResult]] = {}

        # 策略评估结果：pair_id -> {strategy_name -> bool}
        self._strategy_results: dict[str, dict[str, bool]] = {}

        # 机会列表
        self._opportunities: list[dict[str, Any]] = []
        self._max_opportunities = 100

        # 回调
        self._opportunity_callbacks: list[Callable[[dict], None]] = []

    # =========================================================================
    # 配置管理
    # =========================================================================

    @property
    def config(self) -> StrategyServiceConfig:
        """获取配置"""
        return self._config

    def update_config(self, config: StrategyServiceConfig) -> None:
        """更新配置"""
        self._config = config
        self._log.info("Strategy config updated")

    # =========================================================================
    # 比赛上下文管理
    # =========================================================================

    def register_match(
        self,
        pair_id: str,
        competition: str,
        home_team: str,
        away_team: str,
        is_live: bool = False,
    ) -> None:
        """
        注册比赛信息

        在订阅赔率前调用，设置比赛的基本信息。

        Args:
            pair_id: 比赛 ID
            competition: 联赛名称
            home_team: 主队名称
            away_team: 客队名称
            is_live: 是否为赛中盘
        """
        self._match_contexts[pair_id] = MatchContext(
            pair_id=pair_id,
            competition=competition,
            home_team=home_team,
            away_team=away_team,
            is_live=is_live,
        )
        self._log.debug(f"Registered match: {pair_id} ({home_team} vs {away_team})")

    def update_match_status(self, pair_id: str, is_live: bool) -> None:
        """
        更新比赛状态（赛前/赛中）

        Args:
            pair_id: 比赛 ID
            is_live: 是否为赛中盘
        """
        if pair_id in self._match_contexts:
            self._match_contexts[pair_id].is_live = is_live
            self._log.debug(f"Match {pair_id} status: {'live' if is_live else 'pre-match'}")

    # =========================================================================
    # 赔率更新回调
    # =========================================================================

    def on_odds_update(self, pair_id: str, venue: str, odds_data: dict) -> None:
        """
        赔率更新回调

        由 OddsSubscriptionService 调用。

        Args:
            pair_id: 比赛 ID
            venue: 平台 ("polymarket" | "orbitexch")
            odds_data: 赔率数据
        """
        if not self._config.enabled:
            return

        # 更新赔率缓存
        if pair_id not in self._odds_cache:
            self._odds_cache[pair_id] = {"polymarket": {}, "orbitexch": {}}

        # 合并赔率数据
        market_type = odds_data.get("market_type", "")
        if market_type:
            self._odds_cache[pair_id][venue][market_type] = odds_data

        # 更新 MatchContext 中的赔率
        if pair_id in self._match_contexts:
            ctx = self._match_contexts[pair_id]
            ctx.polymarket_odds = self._odds_cache[pair_id].get("polymarket", {})
            ctx.orbitexch_odds = self._odds_cache[pair_id].get("orbitexch", {})

        # 只有两个平台都有数据时才计算
        poly_odds = self._odds_cache[pair_id].get("polymarket", {})
        orbit_odds = self._odds_cache[pair_id].get("orbitexch", {})

        if poly_odds and orbit_odds:
            self._evaluate_match(pair_id)

    # =========================================================================
    # 策略评估
    # =========================================================================

    def _evaluate_match(self, pair_id: str) -> None:
        """
        评估比赛的所有策略

        Args:
            pair_id: 比赛 ID
        """
        # 获取比赛上下文
        context = self._match_contexts.get(pair_id)
        if not context:
            # 没有注册比赛信息，创建一个默认的
            context = MatchContext(
                pair_id=pair_id,
                polymarket_odds=self._odds_cache[pair_id].get("polymarket", {}),
                orbitexch_odds=self._odds_cache[pair_id].get("orbitexch", {}),
            )
            self._match_contexts[pair_id] = context

        # 清空上一次的套利方向
        context.clear_directions()

        # 获取该比赛应使用的策略列表
        strategies = self._config.get_strategies_for_match(
            context.competition,
            context.home_team,
            context.away_team,
        )

        # 初始化结果缓存
        if pair_id not in self._signal_results:
            self._signal_results[pair_id] = {}
        if pair_id not in self._strategy_results:
            self._strategy_results[pair_id] = {}

        # 评估每个策略（信号顺序执行）
        any_triggered = False
        for strategy_name in strategies:
            triggered = self._evaluate_strategy_sequential(pair_id, strategy_name, context)
            if triggered:
                any_triggered = True

        # 如果有策略触发，创建机会
        if any_triggered:
            self._create_opportunity(pair_id, strategies)

    def _evaluate_strategy_sequential(
        self,
        pair_id: str,
        strategy_name: str,
        context: MatchContext,
    ) -> bool:
        """
        顺序评估策略中的信号

        使用 Strategy 类执行策略，支持自定义方向选择逻辑。
        策略中的信号按顺序执行，任意信号返回 satisfied=False 则停止。
        所有信号满足后，调用策略的 select_direction 方法选择最优方向。

        Args:
            pair_id: 比赛 ID
            strategy_name: 策略名称
            context: 比赛上下文

        Returns:
            策略是否触发
        """
        # 获取策略定义
        strategy_def = self._config.get_strategy(strategy_name)
        if not strategy_def:
            self._log.warning(f"Unknown strategy config: {strategy_name}")
            return False

        # 获取策略类实例
        strategy = self._get_strategy_instance(strategy_name, strategy_def.signals)

        # 创建信号计算器
        def signal_calculator(signal_name: str, ctx: MatchContext) -> SignalResult | None:
            return self._calculate_signal(pair_id, signal_name, ctx)

        # 执行策略
        result = strategy.execute(context, signal_calculator)

        # 缓存信号结果
        for name, sig_result in result.signal_results.items():
            self._signal_results[pair_id][name] = sig_result

        self._strategy_results[pair_id][strategy_name] = result.triggered

        if result.triggered:
            self._log.info(
                f"Strategy {strategy_name} triggered for {pair_id}, "
                f"selected_direction={result.selected_direction.direction_id if result.selected_direction else None}"
            )
        else:
            self._log.debug(f"Strategy {strategy_name} not triggered for {pair_id}")

        return result.triggered

    def _get_strategy_instance(
        self,
        strategy_name: str,
        signals: list[str],
    ) -> Strategy:
        """
        获取策略实例

        优先从注册表查找策略类，未找到则使用 DefaultStrategy。

        Args:
            strategy_name: 策略名称
            signals: 信号列表

        Returns:
            策略实例
        """
        # 尝试从注册表获取策略类
        strategy_class = get_strategy_class(strategy_name)

        if strategy_class:
            return strategy_class(name=strategy_name, signals=signals)
        else:
            # 使用默认策略
            return DefaultStrategy(name=strategy_name, signals=signals)

    def _calculate_signal(
        self,
        pair_id: str,
        signal_name: str,
        context: MatchContext,
    ) -> SignalResult | None:
        """
        计算单个信号

        Args:
            pair_id: 比赛 ID
            signal_name: 信号名称
            context: 比赛上下文

        Returns:
            信号计算结果
        """
        signal = get_signal(signal_name)
        if not signal:
            self._log.warning(f"Unknown signal: {signal_name}")
            return None

        # 获取参数（可能被比赛配置覆盖）
        params = self._config.get_signal_params(
            signal_name,
            context.competition,
            context.home_team,
            context.away_team,
        )

        try:
            result = signal.calculate(context, params)
            self._signal_results[pair_id][signal_name] = result

            self._log.debug(
                f"Signal {signal_name} for {pair_id}: "
                f"satisfied={result.satisfied}, value={result.value}"
            )

            return result

        except Exception as e:
            self._log.error(f"Failed to calculate signal {signal_name} for {pair_id}: {e}")
            return None

    def _create_opportunity(
        self,
        pair_id: str,
        strategies: list[str],
    ) -> None:
        """
        创建套利机会

        Args:
            pair_id: 比赛 ID
            strategies: 触发的策略列表
        """
        context = self._match_contexts.get(pair_id)
        signals = self._signal_results.get(pair_id, {})
        strategy_results = self._strategy_results.get(pair_id, {})

        # 获取触发的策略
        triggered_strategies = [
            name for name in strategies
            if strategy_results.get(name, False)
        ]

        if not triggered_strategies:
            return

        # 获取 rebate 信号的值（如果有）
        rebate_signal = signals.get("rebate")
        rebate_value = rebate_signal.value if rebate_signal else None

        # 获取最佳套利方向
        best_direction = context.get_best_direction() if context else None
        all_directions = [d.to_dict() for d in context.arbitrage_directions] if context else []

        opportunity = {
            "opportunity_id": f"opp-{pair_id}-{int(time.time() * 1000)}",
            "pair_id": pair_id,
            "competition": context.competition if context else "",
            "home_team": context.home_team if context else "",
            "away_team": context.away_team if context else "",
            "is_live": context.is_live if context else False,
            "detected_at": time.time(),
            "triggered_strategies": triggered_strategies,
            "rebate_value": rebate_value,
            "best_direction": best_direction.to_dict() if best_direction else None,
            "all_directions": all_directions,
            "signals": {name: result.to_dict() for name, result in signals.items()},
            "status": "detected",
        }

        # 添加到机会列表
        self._opportunities.insert(0, opportunity)
        if len(self._opportunities) > self._max_opportunities:
            self._opportunities = self._opportunities[:self._max_opportunities]

        self._log.info(
            f"Opportunity detected for {pair_id}: "
            f"strategies={triggered_strategies}, rebate={rebate_value}, "
            f"directions={len(all_directions)}"
        )

        # 触发回调
        for callback in self._opportunity_callbacks:
            try:
                callback(opportunity)
            except Exception as e:
                self._log.error(f"Opportunity callback error: {e}")

    # =========================================================================
    # 回调注册
    # =========================================================================

    def register_opportunity_callback(
        self,
        callback: Callable[[dict], None],
    ) -> None:
        """注册机会检测回调"""
        self._opportunity_callbacks.append(callback)

    # =========================================================================
    # 数据查询
    # =========================================================================

    def get_signals(self, pair_id: str | None = None) -> dict[str, Any]:
        """
        获取信号量状态

        Args:
            pair_id: 比赛 ID，None 表示获取所有

        Returns:
            信号量数据
        """
        if pair_id:
            signals = self._signal_results.get(pair_id, {})
            return {
                "pair_id": pair_id,
                "signals": {
                    name: result.to_dict()
                    for name, result in signals.items()
                },
            }
        else:
            return {
                pid: {
                    name: result.to_dict()
                    for name, result in signals.items()
                }
                for pid, signals in self._signal_results.items()
            }

    def get_strategy_results(self, pair_id: str | None = None) -> dict[str, Any]:
        """
        获取策略评估结果

        Args:
            pair_id: 比赛 ID，None 表示获取所有

        Returns:
            策略结果
        """
        if pair_id:
            return {
                "pair_id": pair_id,
                "strategies": self._strategy_results.get(pair_id, {}),
            }
        else:
            return self._strategy_results

    def get_opportunities(
        self,
        limit: int = 50,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        获取检测到的机会

        Args:
            limit: 返回数量限制
            status: 状态过滤

        Returns:
            机会列表
        """
        opportunities = self._opportunities

        if status:
            opportunities = [o for o in opportunities if o.get("status") == status]

        return opportunities[:limit]

    def get_match_context(self, pair_id: str) -> MatchContext | None:
        """获取比赛上下文"""
        return self._match_contexts.get(pair_id)

    def get_arbitrage_directions(self, pair_id: str | None = None) -> dict[str, Any]:
        """
        获取套利方向

        Args:
            pair_id: 比赛 ID，None 表示获取所有

        Returns:
            套利方向数据
        """
        if pair_id:
            context = self._match_contexts.get(pair_id)
            if not context:
                return {"pair_id": pair_id, "directions": [], "best_direction": None}

            best = context.get_best_direction()
            return {
                "pair_id": pair_id,
                "directions": [d.to_dict() for d in context.arbitrage_directions],
                "best_direction": best.to_dict() if best else None,
            }
        else:
            result = {}
            for pid, ctx in self._match_contexts.items():
                if ctx.arbitrage_directions:
                    best = ctx.get_best_direction()
                    result[pid] = {
                        "directions": [d.to_dict() for d in ctx.arbitrage_directions],
                        "best_direction": best.to_dict() if best else None,
                    }
            return result

    def clear_cache(self) -> None:
        """清除所有缓存"""
        self._match_contexts.clear()
        self._odds_cache.clear()
        self._signal_results.clear()
        self._strategy_results.clear()
        self._opportunities.clear()
        self._log.info("Strategy caches cleared")
