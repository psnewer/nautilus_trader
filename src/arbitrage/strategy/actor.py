"""
StrategyEvaluator —— NT Actor,唯一活体(Q21):订触发事件 → 更新 SignalStore → 查
StrategyRegistry → 并行 evaluate arb+comp → fire(套利优先)。

设计见 `docs/arbitrage/architectures/strategy/architecture.md §3.5 / §4`。

调用关系:
- `on_data(data)` —— NT msgbus 路由进:OrderBookDeltas / MatchedPair / 自定义信号事件
- `_extract_evaluation_target(data)` —— 拿到 (pair_id, sport, competition);MatchedPair 直读,
  其它 event 经 PairRegistry + instrument.info 反查
- `_evaluate_and_fire(strategy, pair_id)` —— Q19 让路检查 + Q20 snapshot + asyncio.gather
  并行 evaluate + 套利优先 fire(fire-and-forget)

**evaluate 无副作用 / fire 在顶层**:`evaluate_tree` 返 EvalResult,本类决定 fire arb 还是 comp。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.actor import ActorConfig
from nautilus_trader.model.data import DataType
from nautilus_trader.model.data import OrderBookDeltas

from src.arbitrage.common.pair_registry import PairRegistry
from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate
from src.arbitrage.matching.events import MatchedPair
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.condition import evaluate_tree
from src.arbitrage.strategy.registry import StrategyRegistry
from src.arbitrage.strategy.signals import SignalStore
from src.arbitrage.strategy.snapshot import build_snapshot


_STRATEGY_ID_LITERAL = "ARB-EVAL-001"


def make_submitter(*, cache, msgbus, clock, trader_id, log):
    """slice 10a(#50)module-level 工厂:返 `async def submit(spec)` callable。

    `spec` schema:`{instrument_id, side: "BUY"|"SELL", qty: float, price: float}`

    流程:`spec` → 经 `cache.instrument(iid).{size_precision,price_precision}` 构 NT `LimitOrder`
    → 包成 `SubmitOrder` cmd → `msgbus.send("ExecEngine.execute", cmd)` → NT ExecEngine 路由到
    venue ExecClient(若 debug.skip_execution=True,SkipExecutionClient 拦截 mock 全成)。

    `cache.instrument(iid)` 返 None(冷启动 / 未订阅)→ 跳过 + warning,不 raise。
    """
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.execution.messages import SubmitOrder
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.enums import TimeInForce
    from nautilus_trader.model.identifiers import ClientOrderId
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.identifiers import StrategyId
    from nautilus_trader.model.objects import Price
    from nautilus_trader.model.objects import Quantity
    from nautilus_trader.model.orders import LimitOrder

    strategy_id = StrategyId(_STRATEGY_ID_LITERAL)

    async def submit(spec: dict) -> None:
        raw_iid = spec["instrument_id"]
        iid = raw_iid if isinstance(raw_iid, InstrumentId) else InstrumentId.from_str(str(raw_iid))
        inst = cache.instrument(iid)
        if inst is None:
            log.warning(f"submit: instrument {iid} not in cache; skip")
            return
        side_str = spec.get("side", "BUY").upper()
        order_side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL
        order = LimitOrder(
            trader_id=trader_id,
            strategy_id=strategy_id,
            instrument_id=iid,
            client_order_id=ClientOrderId(f"ARB-{UUID4().value[:8]}"),
            order_side=order_side,
            quantity=Quantity(float(spec["qty"]), precision=inst.size_precision),
            price=Price(float(spec["price"]), precision=inst.price_precision),
            time_in_force=TimeInForce.GTC,
            init_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        )
        cmd = SubmitOrder(
            trader_id=trader_id,
            strategy_id=strategy_id,
            position_id=None,
            order=order,
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        )
        msgbus.send("ExecEngine.execute", cmd)

    return submit


class StrategyEvaluatorConfig(ActorConfig, frozen=True, kw_only=True):
    """目前无运行时参数;预留 actor 标识 / 调试开关。"""

    log_evaluations: bool = False


@dataclass(slots=True)
class _RuntimeDeps:
    """非 msgspec config 可放对象的字段(launcher 经 ArbContext 注入)。"""

    pair_registry: PairRegistry
    strategy_registry: StrategyRegistry
    portfolio: object                      # ArbitragePortfolio(有 way_rebate)
    signal_store: SignalStore
    is_execution_active: Callable[[], bool]  # Q19/§6.10:在飞跳过
    loop: object                            # asyncio.AbstractEventLoop
    signal_collector: Callable[[object, SignalStore], None] | None = None  # event → SignalStore 更新(可选)
    pair_inflight: object = None            # PairInFlightGate(§6.10 §7,per-pair 串行);None → 不串行(测试/降级)
    pair_inflight_max_hold_secs: float = 60.0  # in-flight 陈旧自愈上界(> 单笔套利最长耗时)
    leg_settled: object = None              # LegSettledRegistry(§6.10 §7:健检兜底 clear_all 的 arb 在飞判据)


class StrategyEvaluator(Actor):
    def __init__(self, config: StrategyEvaluatorConfig, deps: _RuntimeDeps) -> None:
        super().__init__(config=config)
        self._pair_registry = deps.pair_registry
        self._strategy_registry = deps.strategy_registry
        self._portfolio = deps.portfolio
        self._signal_store = deps.signal_store
        self._is_execution_active = deps.is_execution_active
        self._signal_collector = deps.signal_collector
        self._loop = deps.loop
        self._log_evaluations = config.log_evaluations
        self._obd_subscribed: set[str] = set()  # slice 10e:已订 OBD 的 instrument_id(去重)
        self._pair_inflight = deps.pair_inflight  # §6.10 §7:per-pair 串行闸(None → 不串行)
        self._pair_inflight_max_hold_ns = int(deps.pair_inflight_max_hold_secs * 1e9)
        self._leg_settled = deps.leg_settled      # §6.10 §7:健检兜底 clear_all 的判据
        self._hc_running: set[str] = set()        # §6.10:在跑的健康检查 source 集合(per-venue,非 ref-count)

    # ── 生命周期 ─────────────────────────────────────────────────────
    def on_start(self) -> None:
        # slice 10d(#52)修 Gap A:`subscribe_data` 强制 SubscribeData cmd 路由,需 client_id/instrument_id;
        # custom Actor-to-Actor 事件用 `msgbus.subscribe(topic)` 直订。
        # NT publish_data 经 `data.{DataType.topic}` 发布,无 metadata 时带尾部 `*`(= `data.MatchedPair*`);
        # 订阅串必须带同款 `*`,否则精确串不匹配带星 publish topic → on_data 永不触发。
        self._msgbus.subscribe(
            topic=f"data.{MatchedPair.__name__}*",
            handler=self.on_data,
        )
        # #60:比分信号 —— 订 PM Sports `SportsGameUpdate`(firehose)→ on_data → `signal_collector`
        # 消化进 SignalStore 供条件树用(collector 为用户域 slice 9;未设时 on_data 对其 no-op,
        # _extract_evaluation_target 返 None 不触发评估)。兑现下方"比分/比赛开始信号接入点"。
        self._msgbus.subscribe(
            topic=f"data.{SportsGameUpdate.__name__}*",
            handler=self.on_data,
        )
        # §6.10:strategy ⊥ 健康检查互斥 —— 订 `health_check.*`(OE/PM 各发各的,带 source)。
        # 在跑期间放弃 fire(避免下单撞到正在 reload 的页面);全部跑完 + leg_settled 全 true → 兜底 clear_all。
        self._msgbus.subscribe(topic="health_check.started", handler=self._on_health_check_started)
        self._msgbus.subscribe(topic="health_check.finished", handler=self._on_health_check_finished)
        # slice 10e:OBD 不在 on_start 预订(无 instrument-level 订阅可言)——改 **MatchedPair fire 后
        # per-iid `subscribe_order_book_deltas(iid)`**(`_ensure_obd_subscribed`),真实赔率进 cache;
        # 订阅的 OBD 由 NT 投到 `on_order_book_deltas` → `_route_eval`(OBD-driven 重评)。

    # ── data 入口(NT 路由)──────────────────────────────────────────
    def on_data(self, data) -> None:
        # 1. SignalCollector 先消化事件(信号写入 store;sports 比分 / 自定义信号)
        if self._signal_collector is not None:
            self._signal_collector(data, self._signal_store)
        # 2. slice 10e:MatchedPair fire → per-iid 订阅 OBD,把真实赔率引进 cache(下游 snapshot 读)
        if isinstance(data, MatchedPair):
            self._ensure_obd_subscribed(data)
        # 3. 路由评估
        self._route_eval(data)

    def on_order_book_deltas(self, deltas) -> None:
        # slice 10e:OBD-driven 重评 —— 订阅的 OBD 由 NT 投到此回调;经 instrument_id→PairRegistry→pair_id 评估
        self._route_eval(deltas)

    # ── §6.10:健康检查互斥 + per-pair 闸兜底 clear_all ────────────────
    def _on_health_check_started(self, msg) -> None:
        """某 venue 健康检查 tick 开始 → 记其 source 在跑(per-venue 信号位,非 ref-count)。"""
        src = (msg or {}).get("source")
        if src is not None:
            self._hc_running.add(src)

    def _on_health_check_finished(self, msg) -> None:
        """某 venue 健康检查 tick 结束 → 移除其 source。

        移除后若**全部健康检查都不在跑** 且 `leg_settled` 全 true(无腿「已发未确认」=确无 arb 在飞)→
        `pair_inflight.clear_all()` 兜底清掉异常泄漏的 per-pair 闸(§6.10 §7,#85)。"""
        src = (msg or {}).get("source")
        if src is not None:
            self._hc_running.discard(src)
        if self._hc_running:
            return  # 还有别的健康检查在跑,不清
        if self._pair_inflight is None:
            return
        if self._leg_settled is not None and self._leg_settled.has_any_unsettled():
            return  # 有腿「已发未确认」→ 有 arb 真在飞,不清
        self._pair_inflight.clear_all()

    def _route_eval(self, data) -> None:
        target = self._extract_evaluation_target(data)
        if target is None:
            return
        pair_id, sport, competition = target
        # scope-priority 查策略(挂载存在锁定,Q21-a)
        strategy = self._strategy_registry.get_for(pair_id, competition, sport)
        if strategy is None:
            if self._log_evaluations:
                self._log.info(
                    f"Strategy evaluate skipped: pair_id={pair_id}, sport={sport}, "
                    f"competition={competition}, reason=no_strategy",
                )
            return
        # §6.10:strategy ⊥ 健康检查互斥 —— 任一健康检查在跑 → 放弃 fire(避免下单撞正在 reload 的页面)。
        if self._hc_running:
            if self._log_evaluations:
                self._log.info(f"Strategy evaluate skipped: pair_id={pair_id}, reason=health_check_active")
            return
        if self._log_evaluations:
            event_type = type(data).__name__
            self._log.info(
                f"Strategy evaluate scheduled: pair_id={pair_id}, sport={sport}, "
                f"competition={competition}, event={event_type}",
            )
        # §6.10 §7:per-pair 串行 —— 同步 acquire(`create_task` 前,首个 await 前)。
        # 同 pair 已在飞(评估中/执行中)→ 直接放弃,不派发评估。单 loop 串行保证同突发后到的评估立刻看到。
        if self._pair_inflight is not None and not self._pair_inflight.try_enter(
            pair_id, self.clock.timestamp_ns(), self._pair_inflight_max_hold_ns,
        ):
            if self._log_evaluations:
                self._log.info(f"Strategy evaluate skipped: pair_id={pair_id}, reason=pair_in_flight")
            return
        # sync 入口 → async evaluate
        self._loop.create_task(self._evaluate_and_fire(strategy, pair_id))

    def _ensure_obd_subscribed(self, mp: MatchedPair) -> None:
        """slice 10e:MatchedPair 的两边各腿首次见到时订阅 OrderBookDeltas(去重)。
        instrument 已在 cache(slice A 发现);OE/PM data client `_subscribe_order_book_deltas` 接 WS 流。"""
        from nautilus_trader.model.identifiers import InstrumentId

        for iid_str in list(mp.pm_instrument_ids) + list(mp.oe_instrument_ids):
            if iid_str in self._obd_subscribed:
                continue
            self._obd_subscribed.add(iid_str)
            try:
                self.subscribe_order_book_deltas(InstrumentId.from_str(iid_str))
            except Exception as e:  # noqa: BLE001 — 单腿订阅失败不挡其它
                self._log.warning(f"OBD subscribe {iid_str} failed: {e!r}")

    # ── 评估主流程 ────────────────────────────────────────────────────
    async def _evaluate_and_fire(self, strategy, pair_id: str) -> None:
        # §6.10 §7:per-pair 闸已在 `_route_eval` 同步 acquire。未 fire(让路/无机会/异常)→ finally 释放;
        # 已 fire → 不释放,所有权交执行(execution `exec_finished` 在双腿 session 归 0 时清)。
        fired = False
        try:
            # Q19:执行在飞 → 直接让路(策略前置 pre-check 放弃机会)
            if self._is_execution_active():
                if self._log_evaluations:
                    self._log.info(f"Strategy evaluate skipped: pair_id={pair_id}, reason=execution_active")
                return
            # Q20:取一次 snapshot,整轮评估 + Action 决策用同一份(safety gate 走 live)
            snapshot = build_snapshot(
                pair_id, cache=self.cache, portfolio=self._portfolio, pair_registry=self._pair_registry,
            )
            # slice 9(#49):store 走 per-pair view(P3 隔离);scratch 由 EvalContext default_factory 创建(per-eval 自动隔离 Check→Action 传值)
            # slice 10a(#50):submitter 注入 — Action 经 `await ctx.submitter(spec)` 真出单
            ctx = EvalContext(
                pair_id=pair_id,
                snapshot=snapshot,
                store=self._signal_store.view(pair_id),
                submitter=self._make_submitter(),
            )
            # 并行 evaluate(纯求值,无副作用);asyncio.gather 让 evaluator 顶层能等两树都返才决定 fire
            arb_res, comp_res = await asyncio.gather(
                self._aevaluate(strategy.arbitrage_tree, ctx),
                self._aevaluate(strategy.compensation_tree, ctx),
            )
            if self._log_evaluations:
                self._log.info(
                    f"Strategy evaluate result: pair_id={pair_id}, arb_hit={arb_res.hit}, "
                    f"arb_action={type(arb_res.pending_action).__name__ if arb_res.pending_action else None}, "
                    f"comp_hit={comp_res.hit}, "
                    f"comp_action={type(comp_res.pending_action).__name__ if comp_res.pending_action else None}",
                )
            # Q21:套利优先 — 套利命中 → fire 套利 action;否则 → 补救 action(如命中)
            if arb_res.hit and arb_res.pending_action is not None:
                fired = True
                if self._log_evaluations:
                    self._log.info(f"Strategy action fired: pair_id={pair_id}, action=arbitrage")
                self._loop.create_task(arb_res.pending_action.execute(ctx))
            elif comp_res.hit and comp_res.pending_action is not None:
                fired = True
                if self._log_evaluations:
                    self._log.info(f"Strategy action fired: pair_id={pair_id}, action=compensation")
                self._loop.create_task(comp_res.pending_action.execute(ctx))
            elif self._log_evaluations:
                self._log.info(f"Strategy action skipped: pair_id={pair_id}, reason=no_pending_action")
        finally:
            if not fired and self._pair_inflight is not None:
                self._pair_inflight.release_eval(pair_id)

    async def _aevaluate(self, tree, ctx):
        """async 包 sync 的 `evaluate_tree`,使 `asyncio.gather` 模式可用。
        Check 实现层若需要 async I/O(查外部服务),可演进为真正 async 求值;现框架是 sync。"""
        return evaluate_tree(tree, ctx)

    # ── slice 10a(#50):submitter 工厂 ───────────────────────────────
    def _make_submitter(self):
        """返 async callable:Action 经 `await ctx.submitter(spec)` 提交订单。

        thin wrapper 经 module-level `make_submitter(...)`,便于单测 mock 各 dep。
        """
        return make_submitter(
            cache=self.cache,
            msgbus=self.msgbus,
            clock=self._clock,
            trader_id=self.trader_id,
            log=self._log,
        )

    # ── 提取评估目标(支持 OrderBookDeltas / MatchedPair / 其它)──────
    def _extract_evaluation_target(self, data) -> tuple[str, str | None, str | None] | None:
        """返 (pair_id, sport, competition) 或 None(跳过该 event)。"""
        # MatchedPair:直接带 pair_id/sport/competition
        if isinstance(data, MatchedPair):
            return (data.pair_id, data.sport, data.competition)
        # 通用:经 instrument_id → PairRegistry → pair_id;sport/comp 从 instrument.info
        iid = getattr(data, "instrument_id", None)
        if iid is None:
            return None
        pair_id = self._pair_registry.get(iid)
        if pair_id is None:
            return None
        inst = self.cache.instrument(iid)
        if inst is None or not getattr(inst, "info", None):
            return (pair_id, None, None)
        return (pair_id, inst.info.get("sport"), inst.info.get("competition"))
