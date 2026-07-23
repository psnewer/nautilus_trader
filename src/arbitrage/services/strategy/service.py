"""
套利策略服务

负责信号量计算、策略评估和机会检测。
支持插拔式的 Signal 和 Strategy 配置。
"""

import logging
import time
from typing import Any, TYPE_CHECKING

from .config import StrategyServiceConfig, MatchConfig
from .signals import get_signal, SignalResult, MatchContext
from .signals.base import ArbitrageDirection
from .strategies import Strategy, StrategyResult, DefaultStrategy, get_strategy_class
from src.arbitrage.services.odds_subscription.messages import OddsUpdateMessage, MatchStatusMessage
from src.arbitrage.services.odds_subscription.topics import (
    ODDS_TOPIC_PATTERN,
    MATCH_STATUS_TOPIC_PATTERN,
)
from src.arbitrage.services.strategy.messages import OpportunityMessage
from src.arbitrage.services.strategy.topics import opportunity_topic

if TYPE_CHECKING:
    from src.arbitrage.services.odds_subscription.service import OddsSubscriptionService


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
        odds_service: "OddsSubscriptionService | None" = None,
        risk_service: "Any | None" = None,
        share: float = 100.0,
        msgbus=None,
    ):
        self._config = config or StrategyServiceConfig()
        self._log = logger or logging.getLogger(self.__class__.__name__)

        # 赔率服务引用（用于获取持仓数据）
        self._odds_service = odds_service

        # 风控服务引用
        self._risk_service = risk_service

        # 套利参数
        self._share = share
        self._fx = 1.0

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

        # 信号分类
        self._odds_signals = {"rebate", "mean_rebate", "multi-way"}
        self._status_signals = {"live"}


        # 消息总线
        self._msgbus = None
        if msgbus is not None:
            self.set_msgbus(msgbus)

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

    def set_odds_service(self, odds_service: "OddsSubscriptionService") -> None:
        """设置赔率服务引用"""
        self._odds_service = odds_service
        self._log.info("Odds service reference set")

    def set_msgbus(self, msgbus) -> None:
        """设置消息总线并订阅主题"""
        if self._msgbus is msgbus:
            return
        self._msgbus = msgbus
        if not self._msgbus:
            return
        self._msgbus.subscribe(ODDS_TOPIC_PATTERN, self._on_odds_message)
        self._msgbus.subscribe(MATCH_STATUS_TOPIC_PATTERN, self._on_match_status_message)

    def _on_odds_message(self, msg: Any) -> None:
        """处理赔率更新消息"""
        if isinstance(msg, OddsUpdateMessage):
            self.on_odds_update(msg.pair_id, msg.venue, msg.odds_data)
        elif isinstance(msg, dict):
            pair_id = msg.get("pair_id", "")
            venue = msg.get("venue", "")
            odds_data = msg.get("odds_data", {})
            if pair_id and venue and odds_data:
                self.on_odds_update(pair_id, venue, odds_data)

    def _on_match_status_message(self, msg: Any) -> None:
        """处理比赛状态消息"""
        if isinstance(msg, MatchStatusMessage):
            self.update_match_status(msg.pair_id, msg.is_live)
        elif isinstance(msg, dict):
            pair_id = msg.get("pair_id", "")
            if pair_id and "is_live" in msg:
                self.update_match_status(pair_id, bool(msg.get("is_live")))

    def set_risk_service(self, risk_service) -> None:
        """设置风控服务引用"""
        self._risk_service = risk_service
        self._log.info("Risk service reference set")

    def set_arbitrage_params(self, share: float, fx: float = 1.0) -> None:
        """
        设置套利参数

        Args:
            share: 用于计算 way_rebate 的基数
            fx: GBP/USD 汇率
        """
        self._share = share
        self._fx = fx
        self._log.info(f"Arbitrage params updated: share={share}, fx={fx}")

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
            self._log.debug(f"Match {pair_id} status: {'live' if is_live else 'not-live'}")
            # 状态变化触发状态信号计算
            self._evaluate_match(pair_id, triggered_signals=self._status_signals)

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

        # 赔率更新只触发赔率相关信号计算
        if pair_id in self._match_contexts:
            self._evaluate_match(pair_id, triggered_signals=self._odds_signals)
        else:
            self._log.debug(f"Pair {pair_id} not in match_contexts, skipping evaluation")

    # =========================================================================
    # 策略评估
    # =========================================================================

    def _evaluate_match(self, pair_id: str, triggered_signals: set[str] | None = None) -> bool:
        """
        评估比赛的所有策略

        Args:
            pair_id: 比赛 ID
        """
        # 检查 pair 是否被其他服务锁定（防竞态：消息已在队列中时 pair 被锁定）
        if self._odds_service and self._odds_service._is_pair_active(pair_id):
            self._log.debug(f"Pair {pair_id} is locked, skipping evaluation")
            return False

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

        # 从 RiskService 实时查询 way_rebate
        if self._risk_service:
            try:
                context.way_rebate = self._risk_service.get_way_rebate(pair_id)
                context.way_rebate_by_venue = self._risk_service.get_way_rebate_by_venue(pair_id)
            except Exception as e:
                self._log.warning(f"Failed to get way_rebate for {pair_id}: {e}")
                context.way_rebate = {}
                context.way_rebate_by_venue = {}
        else:
            context.way_rebate = {}
            context.way_rebate_by_venue = {}

        # 获取该比赛应使用的策略列表
        strategies = self._config.get_strategies_for_match(
            context.competition,
            context.home_team,
            context.away_team,
        )

        # Debug 覆盖：strategy_name 激活时强制使用单一策略（用于测试切换策略）
        try:
            from src.arbitrage.services.debug import debug_manager
            if debug_manager.is_override_active("strategy_name"):
                forced = debug_manager.get_override("strategy_name")
                if forced and self._config.get_strategy(forced):
                    strategies = [forced]
                    self._log.debug(f"Strategy override: forced={forced}")
                elif forced:
                    self._log.warning(f"Strategy override {forced} not in config, ignoring")
        except ImportError:
            pass

        # 初始化结果缓存
        if pair_id not in self._signal_results:
            self._signal_results[pair_id] = {}
        if pair_id not in self._strategy_results:
            self._strategy_results[pair_id] = {}

        # 评估每个策略（信号顺序执行）
        any_triggered = False
        for strategy_name in strategies:
            triggered = self._evaluate_strategy_sequential(
                pair_id, strategy_name, context, triggered_signals
            )
            if triggered:
                any_triggered = True

        # 如果有策略触发，创建机会
        if any_triggered:
            return self._create_opportunity(pair_id, strategies)

        return False

    def _evaluate_strategy_sequential(
        self,
        pair_id: str,
        strategy_name: str,
        context: MatchContext,
        triggered_signals: set[str] | None = None,
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
        strategy = self._get_strategy_instance(strategy_name, strategy_def.signals, strategy_def.strict_filter)

        # 创建信号计算器
        def signal_calculator(signal_name: str, ctx: MatchContext) -> SignalResult | None:
            # 非本次触发的信号优先复用缓存
            if triggered_signals is not None and signal_name not in triggered_signals:
                cached = self._signal_results.get(pair_id, {}).get(signal_name)
                if cached is not None:
                    return cached
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
        strict_filter: bool = False,
    ) -> Strategy:
        """
        获取策略实例

        优先从注册表查找策略类，未找到则使用 DefaultStrategy。

        Args:
            strategy_name: 策略名称
            signals: 信号列表
            strict_filter: 优先级筛选严格模式

        Returns:
            策略实例
        """
        # 尝试从注册表获取策略类
        strategy_class = get_strategy_class(strategy_name)

        if strategy_class:
            return strategy_class(name=strategy_name, signals=signals, strict_filter=strict_filter)
        else:
            # 使用默认策略
            return DefaultStrategy(name=strategy_name, signals=signals, strict_filter=strict_filter)

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

        # 为特定信号注入额外参数
        params = self._inject_signal_params(pair_id, signal_name, params)

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

    def _inject_signal_params(
        self,
        pair_id: str,
        signal_name: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """
        为特定信号注入运行时参数

        Args:
            pair_id: 比赛 ID
            signal_name: 信号名称
            params: 原始参数

        Returns:
            增强后的参数
        """
        # rebate / mean_rebate 信号：检查 debug 覆盖
        if signal_name in ("rebate", "mean_rebate"):
            try:
                from src.arbitrage.services.debug import debug_manager
                if debug_manager.is_override_active("min_rebate_rate"):
                    params["rate"] = debug_manager.get_override("min_rebate_rate", params.get("rate", 0.01))
                    self._log.debug(f"{signal_name} rate override: {params['rate']}")
            except ImportError:
                pass

        return params

    def _check_and_adjust_size(
        self,
        pair_id: str,
        direction: ArbitrageDirection | None,
    ) -> float | None:
        """
        检查市场可交易数量并调整 share

        Returns:
            调整后的 share，如果最小值不满足返回 None
        """
        if not direction:
            return self._share

        odds_cache = self._odds_cache.get(pair_id, {})
        share = self._share

        # 组装 legs 数据供 utils 计算
        legs_info = []
        for leg in direction.legs:
            venue = leg.venue.value
            market_type = leg.market_type
            venue_odds = odds_cache.get(venue, {}).get(market_type, {})

            if venue == "polymarket":
                intended = share
                if leg.action.value == "buy":
                    available = venue_odds.get("ask_size", 0)
                else:
                    available = venue_odds.get("bid_size", 0)
                raw_odds = 0
            else:  # orbitexch
                raw_odds = leg.raw_odds
                intended = share / raw_odds if raw_odds > 0 else 0
                if leg.action.value == "buy":
                    available = venue_odds.get("back_size", 0)
                else:
                    available = venue_odds.get("lay_size", 0)

            legs_info.append({
                "venue": venue,
                "intended": intended,
                "available": available,
                "raw_odds": raw_odds,
            })

        adjusted_share = _scale_share_by_liquidity(share, legs_info)

        if adjusted_share < share:
            self._log.info(
                f"Size scaled for {pair_id}: share {share} → {adjusted_share:.2f} "
                f"(factor={adjusted_share / share:.4f})"
            )

        return adjusted_share

    def _create_opportunity(
        self,
        pair_id: str,
        strategies: list[str],
    ) -> bool:
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
            return False

        # 风控检查
        if self._risk_service:
            risk_result = self._risk_service.check_risk(pair_id)
            if not risk_result.allowed:
                self._log.warning(
                    f"Opportunity blocked by risk control for {pair_id}: {risk_result.reason} "
                    f"(skipped)"
                )
                return False

        # 获取 rebate 信号的值（优先 mean_rebate，其次 rebate）
        mean_rebate_signal = signals.get("mean_rebate")
        rebate_signal = signals.get("rebate")

        if mean_rebate_signal and mean_rebate_signal.satisfied:
            rebate_value = mean_rebate_signal.value
            signal_name = "mean_rebate"
        elif rebate_signal:
            rebate_value = rebate_signal.value
            signal_name = "rebate"
        else:
            rebate_value = None
            signal_name = "rebate"

        # 获取最佳套利方向
        best_direction = context.get_best_direction() if context else None
        all_directions = [d.to_dict() for d in context.arbitrage_directions] if context else []

        if not best_direction:
            self._log.info(
                f"No arbitrage direction for {pair_id} "
                f"(skipped)"
            )
            return False

        # ---- Size 可用性检查 ----
        # debug 覆盖 skip_check_size 激活时绕过（用于测试下单链路）
        try:
            from src.arbitrage.services.debug import debug_manager
            skip_size = debug_manager.is_override_active("skip_check_size")
        except ImportError:
            skip_size = False

        if skip_size:
            adjusted_share = self._share
            self._log.debug(f"skip_check_size active, using share={self._share} for {pair_id}")
        else:
            adjusted_share = self._check_and_adjust_size(pair_id, best_direction)
            if adjusted_share is None:
                self._log.warning(
                    f"Opportunity rejected: insufficient market size for {pair_id} "
                    f"(skipped)"
                )
                return False

        # ---- 余额门控检查 ----
        if self._risk_service:
            balance_check = self._risk_service.check_balance(
                pair_id=pair_id,
                share=adjusted_share,
                direction=best_direction
            )
            if not balance_check.allowed:
                self._log.warning(
                    f"Opportunity blocked by balance check for {pair_id}: {balance_check.reason} "
                    f"(skipped)"
                )
                return False

        # 获取各方向的持仓返水率（执行侧用于 size 调整）
        way_rebate = context.way_rebate if context else {}

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
            "way_rebate": way_rebate,
            "best_direction": best_direction.to_dict() if best_direction else None,
            "all_directions": all_directions,
            "signals": {name: result.to_dict() for name, result in signals.items()},
            "adjusted_share": adjusted_share,
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

        # 通过消息总线发布
        return self._publish_opportunity(opportunity)

    def _publish_opportunity(self, opportunity: dict) -> bool:
        """通过消息总线发布套利机会"""
        if not self._msgbus:
            self._log.warning("Cannot publish opportunity: msgbus not set")
            return False

        pair_id = opportunity.get("pair_id", "")
        msg = OpportunityMessage(
            opportunity_id=opportunity["opportunity_id"],
            pair_id=pair_id,
            competition=opportunity.get("competition", ""),
            home_team=opportunity.get("home_team", ""),
            away_team=opportunity.get("away_team", ""),
            is_live=opportunity.get("is_live", False),
            detected_at=opportunity.get("detected_at", 0),
            triggered_strategies=opportunity.get("triggered_strategies", []),
            rebate_value=opportunity.get("rebate_value"),
            way_rebate=opportunity.get("way_rebate", {}),
            best_direction=opportunity.get("best_direction"),
            all_directions=opportunity.get("all_directions", []),
            signals=opportunity.get("signals", {}),
            adjusted_share=opportunity.get("adjusted_share"),
            status=opportunity.get("status", "detected"),
        )
        topic = opportunity_topic(pair_id)
        self._msgbus.publish(topic, msg)

        self._log.debug(f"Published opportunity to {topic}")
        return True

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


def _scale_share_by_liquidity(share: float, legs: list[dict]) -> float:
    """旧 services 私有:只按可用量等比例缩放 share,不做应用层最小额门控。"""
    scale_factor = 1.0
    for leg in legs:
        intended = float(leg.get("intended") or 0.0)
        available = float(leg.get("available") or 0.0)
        if intended > 0 and 0 < available < intended:
            scale_factor = min(scale_factor, available / intended)
    return share * scale_factor
