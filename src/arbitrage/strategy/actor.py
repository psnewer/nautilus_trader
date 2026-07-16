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
from dataclasses import replace
from typing import Callable

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.actor import ActorConfig
from nautilus_trader.model.data import DataType
from nautilus_trader.model.data import OrderBookDeltas

from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.common.control import TOPIC_ARBITRAGE_PARAMS
from src.arbitrage.common.control import SetArbitrageParamsCommand
from src.arbitrage.common.params import ArbitrageParams
from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate
from src.arbitrage.matching.events import MatchedPair
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.condition import evaluate_tree
from src.arbitrage.strategy.registry import StrategyRegistry
from src.arbitrage.strategy.signals import SignalStore
from src.arbitrage.strategy.snapshot import build_snapshot
from src.arbitrage.common.opportunity import OpportunityMeta
from src.arbitrage.common.opportunity import tags_from_meta


_STRATEGY_ID_LITERAL = "ARB-EVAL-001"


def make_submitter(*, cache, msgbus, clock, trader_id, log):
    """slice 10a(#50)module-level 工厂:返 `async def submit(spec)` callable。

    `spec` schema:`{instrument_id, side: "BUY"|"SELL", qty: float, price: float, ...}`

    流程:`spec` → 经 `cache.instrument(iid).{size_precision,price_precision}` 构 NT `LimitOrder`
    → 包成 `SubmitOrder` cmd → `msgbus.send("RiskEngine.execute", cmd)` → Risk pass 后再进
    Execution opportunity barrier / venue ExecClient。

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
        tags = [f"arb:intent={spec.get('intent', 'arbitrage')}"]
        if all(k in spec for k in ("opportunity_id", "pair_id", "leg_key", "expected_legs")):
            tags = tags_from_meta(OpportunityMeta(
                opportunity_id=str(spec["opportunity_id"]),
                pair_id=str(spec["pair_id"]),
                leg_key=str(spec["leg_key"]),
                expected_legs=tuple(str(v) for v in spec["expected_legs"]),
                intent=str(spec.get("intent", "arbitrage")),
                venue_required_balance=spec.get("venue_required_balance"),
            ))
        order = LimitOrder(
            trader_id=trader_id,
            strategy_id=strategy_id,
            instrument_id=iid,
            client_order_id=ClientOrderId(f"ARB-{UUID4().value[:8]}"),
            order_side=order_side,
            quantity=Quantity(float(spec["qty"]), precision=inst.size_precision),
            price=Price(float(spec["price"]), precision=inst.price_precision),
            time_in_force=TimeInForce.GTC,
            tags=tags,
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
        msgbus.send("RiskEngine.execute", cmd)

    return submit


class StrategyEvaluatorConfig(ActorConfig, frozen=True, kw_only=True):
    """目前无运行时参数;预留 actor 标识 / 调试开关。"""

    log_evaluations: bool = False


@dataclass(slots=True)
class _RuntimeDeps:
    """非 msgspec config 可放对象的字段(launcher 经 ArbContext 注入)。"""

    pair_registry: PairRegistry
    strategy_registry: StrategyRegistry
    portfolio: object                      # ArbitragePortfolio
    signal_store: SignalStore
    is_execution_active: Callable[[], bool]  # Q19/§6.10:在飞跳过
    loop: object                            # 单测兜底 loop;生产调度使用 NT register_executor 注入的 ActorExecutor loop
    arbitrage_params: ArbitrageParams | None = None  # Web Arbitrage 运行时默认 share/max_leg_share
    signal_collector: Callable[[object, SignalStore], None] | None = None  # event → SignalStore 更新(可选)
    pair_inflight: object = None            # PairInFlightGate(§6.10 §7,per-pair 串行);None → 不串行(测试/降级)


class StrategyEvaluator(Actor):
    def __init__(self, config: StrategyEvaluatorConfig, deps: _RuntimeDeps) -> None:
        super().__init__(config=config)
        self._pair_registry = deps.pair_registry
        self._strategy_registry = deps.strategy_registry
        self._portfolio = deps.portfolio
        self._signal_store = deps.signal_store
        self._is_execution_active = deps.is_execution_active
        self._arbitrage_params = deps.arbitrage_params or ArbitrageParams()
        self._signal_collector = deps.signal_collector
        self._test_loop = deps.loop
        self._registered_task_loop = None
        self._log_evaluations = config.log_evaluations
        self._obd_subscribed: set[str] = set()  # slice 10e:已订 OBD 的 instrument_id(去重)
        self._pair_inflight = deps.pair_inflight  # §6.10 §7:per-pair 串行闸(None → 不串行)

    # ── 生命周期 ─────────────────────────────────────────────────────
    def register_executor(self, loop, executor) -> None:
        super().register_executor(loop, executor)
        self._registered_task_loop = loop

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
        self._msgbus.subscribe(topic=TOPIC_ARBITRAGE_PARAMS, handler=self._on_set_arbitrage_params_cmd)
        # #108:strategy⊥健康检查互斥(`_hc_running` + `health_check.*`)已退役 —— 旧理由是"健康检查 reload
        # **执行页**会撞下单",但执行页 reload 已迁 NT reconciliation;剩余 competition 页 reload 在另一张页、
        # 且 OE 下单是 page.evaluate(与焦点无关),不冲突。详见 synchronization §8.6 / refactor #108。
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
        if self._log_evaluations:
            event_type = type(data).__name__
            self._log.info(
                f"Strategy evaluate scheduled: pair_id={pair_id}, sport={sport}, "
                f"competition={competition}, event={event_type}",
            )
        # §6.10 §7:per-pair 串行 —— 同步 acquire(`create_task` 前,首个 await 前)。
        # 同 pair 已在飞(评估中/执行中)→ 直接放弃,不派发评估。单 loop 串行保证同突发后到的评估立刻看到。
        if self._pair_inflight is not None and not self._pair_inflight.try_enter(pair_id):
            if self._log_evaluations:
                self._log.info(f"Strategy evaluate skipped: pair_id={pair_id}, reason=pair_in_flight")
            return
        # sync 入口 → async evaluate
        self._create_task(self._evaluate_and_fire(strategy, pair_id))

    def _ensure_obd_subscribed(self, mp: MatchedPair) -> None:
        """slice 10e:MatchedPair 的两边各腿首次见到时订阅 OrderBookDeltas(去重)。
        instrument 已在 cache(slice A 发现);OE/PM data client `_subscribe_order_book_deltas` 接 WS 流。"""
        from nautilus_trader.model.identifiers import InstrumentId

        instrument_ids = list(mp.tradable_instrument_ids)
        for iid_str in instrument_ids:
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
            store = self._signal_store.view(pair_id)
            store.set_transient("pre_match", not bool(getattr(snapshot, "in_play", False)))
            # slice 9(#49):store 走 per-pair view(P3 隔离);scratch 由 EvalContext default_factory 创建。
            # 套利树 / 补偿树必须各自持有独立 scratch:Check 会把 legs 写入 scratch 给同树 Action 消费,
            # 若两树共用 ctx,补偿树单腿会覆盖套利树双腿。
            submitter = self._make_submitter()
            base_ctx = {
                "pair_id": pair_id,
                "snapshot": snapshot,
                "store": store,
                "submitter": submitter,
                "portfolio": self._portfolio,
                "strategy_defaults": self._strategy_defaults(),
            }
            arb_ctx = EvalContext(**base_ctx)
            comp_ctx = EvalContext(**base_ctx)
            # 并行 evaluate;各树 scratch 独立,顶层等两树都返回后再决定 fire
            arb_res, comp_res = await asyncio.gather(
                self._aevaluate(strategy.arbitrage_tree, arb_ctx),
                self._aevaluate(strategy.compensation_tree, comp_ctx),
            )
            if self._log_evaluations:
                arb_actions_str = [type(a).__name__ for a in arb_res.pending_actions] if arb_res.pending_actions else None
                comp_actions_str = [type(a).__name__ for a in comp_res.pending_actions] if comp_res.pending_actions else None
                self._log.info(
                    f"Strategy evaluate result: pair_id={pair_id}, arb_hit={arb_res.hit}, "
                    f"arb_actions={arb_actions_str}, "
                    f"comp_hit={comp_res.hit}, "
                    f"comp_actions={comp_actions_str}",
                )
            # Q21:套利优先 — 套利命中 → fire 套利 actions;否则 → 补救 actions(如命中)
            if arb_res.hit and arb_res.pending_actions:
                fired = True
                if self._log_evaluations:
                    self._log.info(f"Strategy action fired: pair_id={pair_id}, action=arbitrage")
                self._create_task(self._execute_actions(arb_res.pending_actions, arb_ctx))
            elif comp_res.hit and comp_res.pending_actions:
                fired = True
                if self._log_evaluations:
                    self._log.info(f"Strategy action fired: pair_id={pair_id}, action=compensation")
                self._create_task(self._execute_actions(comp_res.pending_actions, comp_ctx))
            elif self._log_evaluations:
                self._log.info(f"Strategy action skipped: pair_id={pair_id}, reason=no_pending_actions")
        finally:
            if not fired and self._pair_inflight is not None:
                self._pair_inflight.release_eval(pair_id)

    def _create_task(self, coro):
        """把 coroutine 投递到 NT kernel 为 Actor 注册的运行 loop。

        NT msgbus handler 是同步调用,`on_data` 不保证处于 asyncio task 内;因此生产路径不能依赖
        `asyncio.get_running_loop()`。`_test_loop` 只用于未注册 executor 的单测 harness。
        """
        loop = self._registered_executor_loop()
        if loop is None:
            return self._test_loop.create_task(coro)

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            return loop.create_task(coro)
        return loop.call_soon_threadsafe(loop.create_task, coro)

    def _registered_executor_loop(self):
        return self._registered_task_loop

    async def _aevaluate(self, tree, ctx):
        """async 包 sync 的 `evaluate_tree`,使 `asyncio.gather` 模式可用。
        Check 实现层若需要 async I/O(查外部服务),可演进为真正 async 求值;现框架是 sync。"""
        return evaluate_tree(tree, ctx)

    async def _execute_actions(self, actions: list, ctx) -> None:
        """依次执行 actions(串行,保证顺序;如 ShareLimitModification → PlaceBetsAction)。"""
        for action in actions:
            await action.execute(ctx)

    def _strategy_defaults(self) -> dict:
        params = self._arbitrage_params
        return {
            "share": params.share,
            "max_leg_share": params.max_leg_share,
        }

    def _on_set_arbitrage_params_cmd(self, cmd) -> None:
        if not isinstance(cmd, SetArbitrageParamsCommand):
            return
        overrides = {
            k: v
            for k, v in (
                ("share", cmd.share),
                ("max_leg_share", cmd.max_leg_share),
                ("fx", cmd.fx),
            )
            if v is not None
        }
        if overrides:
            self._arbitrage_params = replace(self._arbitrage_params, **overrides)

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
