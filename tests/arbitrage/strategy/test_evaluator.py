"""StrategyEvaluator —— Q21 完整集成:on_data → 查 → snapshot → 并行 evaluate → 套利优先 fire。

对应用例:strategy-4.framework.eval.{1-5}
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import TestClock
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.common.params import ArbitrageParams
from src.arbitrage.matching.events import MatchedPair
from src.arbitrage.strategy.actor import StrategyEvaluator
from src.arbitrage.strategy.actor import StrategyEvaluatorConfig
from src.arbitrage.strategy.actor import _RuntimeDeps
from src.arbitrage.strategy.bool_expr import SignalRef
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import Condition
from src.arbitrage.strategy.registry import Strategy
from src.arbitrage.strategy.registry import StrategyRegistry
from src.arbitrage.strategy.signals import SignalStore


# ── 工具:fake loop / actions / portfolio ────────────────────────
class _FakeLoop:
    def __init__(self):
        self.tasks = []
    def create_task(self, coro):
        self.tasks.append(coro)
        return coro


class _RecordingAction(Action):
    """记录 execute 调用(应被 actor 经 create_task 触发)。"""
    def __init__(self, name: str = "act"):
        self.name = name
        self.calls = 0
    async def execute(self, ctx):
        self.calls += 1


class _CaptureDefaultsAction(Action):
    def __init__(self):
        self.defaults = None

    async def execute(self, ctx):
        self.defaults = dict(ctx.strategy_defaults)


class _CaptureSignalAction(Action):
    def __init__(self, key: str):
        self.key = key
        self.value = None

    async def execute(self, ctx):
        self.value = ctx.store.peek(self.key)


class _CaptureScratchLegsAction(Action):
    def __init__(self):
        self.legs = None

    async def execute(self, ctx):
        self.legs = list(ctx.scratch.get("legs") or [])


class _StubCheck(Check):
    def __init__(self, returns: bool):
        self._returns = returns
    def passes(self, ctx):
        return self._returns


class _SetScratchLegsCheck(Check):
    def __init__(self, legs: list[dict]):
        self._legs = legs

    def passes(self, ctx):
        ctx.scratch["legs"] = list(self._legs)
        return True


def _strategy(arb_hit: bool, comp_hit: bool, arb_action=None, comp_action=None) -> Strategy:
    """构造一个策略,arb/comp 树各一叶子,通过 self_hits 信号控制命中。"""
    arb_tree = Condition(
        self_hits=SignalRef("arb_on"),
        checktion=[_StubCheck(True)],
        actions=[arb_action] if arb_action else [],
    )
    comp_tree = Condition(
        self_hits=SignalRef("comp_on"),
        checktion=[_StubCheck(True)],
        actions=[comp_action] if comp_action else [],
    )
    return Strategy(scope_key="pair:match_X", arbitrage_tree=arb_tree, compensation_tree=comp_tree)


def _harness(
    execution_active: bool = False,
    log_evaluations: bool = False,
    pair_inflight=None,
    arbitrage_params=None,
):
    clock = TestClock()
    msgbus = MessageBus(trader_id=TraderId("T-000"), clock=clock)
    cache = TestComponentStubs.cache()
    portfolio = Portfolio(msgbus=msgbus, cache=cache, clock=clock)

    pair_reg = PairRegistry()
    strat_reg = StrategyRegistry()
    store = SignalStore()
    loop = _FakeLoop()

    active_flag = {"v": execution_active}
    deps = _RuntimeDeps(
        pair_registry=pair_reg,
        strategy_registry=strat_reg,
        portfolio=object(),
        signal_store=store,
        is_execution_active=lambda: active_flag["v"],
        loop=loop,
        arbitrage_params=arbitrage_params,
        signal_collector=None,
        pair_inflight=pair_inflight,               # §6.10 §7:per-pair 串行闸(默认 None=不串行)
    )
    actor = StrategyEvaluator(
        StrategyEvaluatorConfig(log_evaluations=log_evaluations),
        deps,
    )
    actor.register_base(portfolio=portfolio, msgbus=msgbus, cache=cache, clock=clock)
    return actor, store, pair_reg, strat_reg, loop, active_flag


async def _drain(loop):
    """跑光 fake loop 中所有 task(evaluate + fire 都走 create_task)。"""
    while loop.tasks:
        coro = loop.tasks.pop(0)
        await coro


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _mp(
    pair_id: str = "match_X",
    *,
    confidence: float = 0,
    tradable_instrument_ids: list[str] | None = None,
    venue_instrument_ids: dict[str, list[str]] | None = None,
    anchor_instrument_ids: list[str] | None = None,
    order_books_managed: bool = False,
) -> MatchedPair:
    """构造当前主 schema 的 MatchedPair;测试不再构造旧 PM/OE 投影字段。"""
    if tradable_instrument_ids is None and venue_instrument_ids is not None:
        tradable_instrument_ids = [iid for ids in venue_instrument_ids.values() for iid in ids]
    return MatchedPair(
        ts_event=0,
        ts_init=0,
        pair_id=pair_id,
        sport="Soccer",
        competition="EPL",
        confidence=confidence,
        anchor_instrument_ids=list(anchor_instrument_ids or []),
        tradable_instrument_ids=list(tradable_instrument_ids or []),
        venue_instrument_ids=dict(venue_instrument_ids or {}),
        order_books_managed=order_books_managed,
    )


# ── eval.1: MatchedPair → 直接拿 pair_id/sport/comp,无需 instrument 反查 ─
def test_matched_pair_routes_directly_with_embedded_pair_id():
    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    arb_action = _RecordingAction("arb")
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=arb_action))
    store.view("match_X").set_persistent("arb_on", True)  # slice 9(#49):P3 per-pair view

    mp = _mp(confidence=1.0)
    actor.on_data(mp)
    _run(_drain(loop))
    assert arb_action.calls == 1


def test_eval_context_strategy_defaults_read_arbitrage_params():
    actor, store, pair_reg, strat_reg, loop, _ = _harness(
        arbitrage_params=ArbitrageParams(share=40.0, max_leg_share=100.0, fx=1.33),
    )
    action = _CaptureDefaultsAction()
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=action))
    store.view("match_X").set_persistent("arb_on", True)

    mp = _mp()
    actor.on_data(mp)
    _run(_drain(loop))

    assert action.defaults == {"share": 40.0, "max_leg_share": 100.0}


def test_evaluator_sets_pre_match_signal_from_snapshot():
    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    action = _CaptureSignalAction("pre_match")
    arb_tree = Condition(
        self_hits=SignalRef("pre_match"),
        checktion=[_StubCheck(True)],
        actions=[action],
    )
    strat_reg.register_pair(
        "match_X",
        Strategy(scope_key="pair:match_X", arbitrage_tree=arb_tree, compensation_tree=Condition(self_hits=SignalRef("never"))),
    )

    mp = _mp()
    actor.on_data(mp)
    _run(_drain(loop))

    assert action.value is True


# ── eval.2: 无挂载 → no-op,不 fire ───────────────────────────────
def test_no_strategy_mounted_no_op():
    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    arb_action = _RecordingAction()
    # 不挂任何策略
    mp = _mp("match_unknown")
    actor.on_data(mp)
    _run(_drain(loop))
    assert arb_action.calls == 0
    assert loop.tasks == []                # 没创建 evaluate task


# ── eval.3: Q19 让路 — execution_active True → 跳过整轮 ──────────
def test_execution_active_skips_evaluation():
    actor, store, pair_reg, strat_reg, loop, active_flag = _harness(execution_active=True)
    arb_action = _RecordingAction()
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=arb_action))
    store.view("match_X").set_persistent("arb_on", True)

    mp = _mp()
    actor.on_data(mp)
    _run(_drain(loop))
    # _evaluate_and_fire 跑了但开头就 return(execution_active)→ 没 fire
    assert arb_action.calls == 0


def test_log_evaluations_enabled_keeps_evaluator_behavior():
    """log_evaluations 只增加评估锚点日志,不改变 Q21 fire 语义。"""
    actor, store, pair_reg, strat_reg, loop, _ = _harness(log_evaluations=True)
    arb_action = _RecordingAction("arb")
    comp_action = _RecordingAction("comp")
    strat_reg.register_pair("match_X", _strategy(True, True, arb_action=arb_action, comp_action=comp_action))
    store.view("match_X").set_persistent("arb_on", True)
    store.view("match_X").set_persistent("comp_on", True)

    mp = _mp()
    actor.on_data(mp)
    _run(_drain(loop))

    assert arb_action.calls == 1
    assert comp_action.calls == 0


def test_log_evaluations_enabled_covers_skip_paths():
    """log_evaluations=True 时无策略 / 执行在飞路径仍保持 no-op。"""
    actor, store, pair_reg, strat_reg, loop, active_flag = _harness(
        execution_active=True,
        log_evaluations=True,
    )
    arb_action = _RecordingAction("arb")
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=arb_action))
    store.view("match_X").set_persistent("arb_on", True)

    actor.on_data(_mp("match_unknown"))
    actor.on_data(_mp())
    _run(_drain(loop))

    assert arb_action.calls == 0
    assert loop.tasks == []


# ── eval.4: Q21 套利优先 — arb 命中 + comp 命中 → 只 fire arb ────
def test_arb_hit_blocks_comp_action():
    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    arb_action = _RecordingAction("arb")
    comp_action = _RecordingAction("comp")
    strat_reg.register_pair("match_X", _strategy(True, True, arb_action=arb_action, comp_action=comp_action))
    store.view("match_X").set_persistent("arb_on", True)
    store.view("match_X").set_persistent("comp_on", True)        # 两边 self_hits 都过

    mp = _mp()
    actor.on_data(mp)
    _run(_drain(loop))
    assert arb_action.calls == 1
    assert comp_action.calls == 0                     # 套利赢


def test_arb_and_comp_evaluation_scratch_is_isolated():
    """套利树与补偿树同轮命中时,套利 action 不得读到补偿树写入的单腿 legs。"""
    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    arb_action = _CaptureScratchLegsAction()
    comp_action = _RecordingAction("comp")
    arb_legs = [
        {"instrument_id": "H.POLYMARKET", "venue": "POLYMARKET", "role": "home"},
        {"instrument_id": "A.ORBITEXCH", "venue": "ORBITEXCH", "role": "away"},
    ]
    comp_legs = [
        {"instrument_id": "A.POLYMARKET", "venue": "POLYMARKET", "role": "away"},
    ]
    arb_tree = Condition(
        self_hits=SignalRef("arb_on"),
        checktion=[_SetScratchLegsCheck(arb_legs)],
        actions=[arb_action],
    )
    comp_tree = Condition(
        self_hits=SignalRef("comp_on"),
        checktion=[_SetScratchLegsCheck(comp_legs)],
        actions=[comp_action],
    )
    strat_reg.register_pair(
        "match_X",
        Strategy(scope_key="pair:match_X", arbitrage_tree=arb_tree, compensation_tree=comp_tree),
    )
    store.view("match_X").set_persistent("arb_on", True)
    store.view("match_X").set_persistent("comp_on", True)

    mp = _mp()
    actor.on_data(mp)
    _run(_drain(loop))

    assert arb_action.legs == arb_legs
    assert comp_action.calls == 0


# ── eval.5: 补救兜底 — arb 没命中 + comp 命中 → fire comp ─────────
def test_comp_hit_when_arb_miss_fires_comp():
    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    arb_action = _RecordingAction("arb")
    comp_action = _RecordingAction("comp")
    strat_reg.register_pair("match_X", _strategy(False, True, arb_action=arb_action, comp_action=comp_action))
    # arb_on 不 set → arb self_hits False;comp_on set → comp self_hits True
    store.view("match_X").set_persistent("comp_on", True)

    mp = _mp()
    actor.on_data(mp)
    _run(_drain(loop))
    assert arb_action.calls == 0
    assert comp_action.calls == 1


# ── 边界:两边都 miss → 无 fire ────────────────────────────────────
def test_both_miss_no_fire():
    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    arb_action = _RecordingAction(); comp_action = _RecordingAction()
    strat_reg.register_pair("match_X", _strategy(False, False, arb_action=arb_action, comp_action=comp_action))
    mp = _mp()
    actor.on_data(mp)
    _run(_drain(loop))
    assert arb_action.calls == 0 and comp_action.calls == 0


# ── SignalCollector 接入:每个 event 先过 collector,再 evaluate ──
def test_signal_collector_called_before_evaluation():
    collector_calls = []
    def my_collector(event, store):
        collector_calls.append(event)
        # collector 拿 raw store(可自行决定 view 范围;此处 fake event 关联 pair "match_X")
        store.view("match_X").set_persistent("arb_on", True)

    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    actor._signal_collector = my_collector            # 注入(也可在 _harness 里设)
    arb_action = _RecordingAction()
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=arb_action))

    mp = _mp()
    actor.on_data(mp)
    _run(_drain(loop))
    assert collector_calls == [mp]                    # collector 收到 event
    assert arb_action.calls == 1                      # signal 写入后,evaluate 命中


# ── slice 10a(#50):EvalContext.submitter 由 evaluator 注入 ─────
def test_submitter_wired_into_eval_context():
    """`_evaluate_and_fire` 构造 EvalContext 时 `submitter=self._make_submitter()`;
    Action 拿到的 ctx.submitter 应是 callable(不是 None)。"""
    captured = []
    class _CaptureCtxAction(Action):
        async def execute(self, ctx):
            captured.append(ctx)

    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    capture_action = _CaptureCtxAction()
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=capture_action))
    store.view("match_X").set_persistent("arb_on", True)

    mp = _mp(confidence=1.0)
    actor.on_data(mp)
    _run(_drain(loop))

    assert len(captured) == 1
    ctx = captured[0]
    assert ctx.submitter is not None              # slice 10a:submitter 已注入
    assert callable(ctx.submitter)


# ── 非目标数据类型(无 pair 信息)→ silently no-op ──────────────
def test_non_routable_data_silently_skipped():
    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    actor.on_data(SimpleNamespace())                   # 既无 instrument_id 也非 MatchedPair
    actor.on_data("garbage")
    actor.on_data(None)
    _run(_drain(loop))
    assert loop.tasks == []                            # 全 skip,无 evaluate task


def test_matched_pair_subscribes_obd_deduped():
    """slice 10e:MatchedPair fire → 两边各腿订 OrderBookDeltas;同 pair 再来不重复订。"""
    actor, *_ = _harness()
    calls = []
    actor.subscribe_order_book_deltas = lambda iid, *a, **k: calls.append((str(iid), k))
    mp = _mp(
        tradable_instrument_ids=["A.PM", "B.PM", "X.OE"],
        venue_instrument_ids={"PM": ["A.PM", "B.PM"], "OE": ["X.OE"]},
        order_books_managed=True,
    )
    assert mp.tradable_instrument_ids == ["A.PM", "B.PM", "X.OE"]
    actor.on_data(mp)
    assert {iid for iid, _ in calls} == {"A.PM", "B.PM", "X.OE"}
    assert all(kwargs == {"managed": False} for _, kwargs in calls)
    actor.on_data(mp)                                  # 再来同 pair → 去重,不再订
    assert len(calls) == 3


def test_matched_pair_without_managed_books_creates_strategy_books():
    """关闭 matching 概率校验时，Strategy 仍以 managed=True 建立 order books。"""
    actor, *_ = _harness()
    calls = []
    actor.subscribe_order_book_deltas = lambda iid, *a, **k: calls.append((str(iid), k))

    actor.on_data(_mp(
        tradable_instrument_ids=["A.PM"],
        venue_instrument_ids={"PM": ["A.PM"]},
        order_books_managed=False,
    ))

    assert calls == [("A.PM", {"managed": True})]


def test_matched_pair_obd_subscription_uses_tradable_ids_not_anchor_ids():
    """strategy-pmsports-anchor.1:PMSPORTS anchor 不参与 OBD 订阅。"""
    actor, *_ = _harness()
    calls = []
    actor.subscribe_order_book_deltas = lambda iid, *a, **k: calls.append(str(iid))
    mp = _mp(
        anchor_instrument_ids=["anchor.PMSPORTS"],
        tradable_instrument_ids=["A.POLYMARKET", "X.ORBITEXCH"],
        venue_instrument_ids={"POLYMARKET": ["A.POLYMARKET"], "ORBITEXCH": ["X.ORBITEXCH"]},
    )

    actor.on_data(mp)

    assert set(calls) == {"A.POLYMARKET", "X.ORBITEXCH"}


def test_matched_pair_obd_subscription_consumes_only_main_fields():
    """venue-registry.10:Strategy 只消费 MatchedPair 主字段。"""
    actor, *_ = _harness()
    calls = []
    actor.subscribe_order_book_deltas = lambda iid, *a, **k: calls.append(str(iid))
    mp = MatchedPair(
        ts_event=0, ts_init=0,
        pair_id="match_X", sport="Soccer", competition="EPL",
        confidence=0,
        tradable_instrument_ids=["A.POLYMARKET", "X.ORBITEXCH"],
        venue_instrument_ids={"POLYMARKET": ["A.POLYMARKET"], "ORBITEXCH": ["X.ORBITEXCH"]},
    )

    actor.on_data(mp)

    assert set(calls) == {"A.POLYMARKET", "X.ORBITEXCH"}


# ── eval.15(§6.10 §7,#84):同 pair 并发触发 → per-pair 闸只放一次 fire ──
def test_same_pair_concurrent_eval_fires_once():
    from src.arbitrage.common.pair_inflight import PairInFlightGate

    gate = PairInFlightGate()
    actor, store, pair_reg, strat_reg, loop, _ = _harness(pair_inflight=gate)
    arb_action = _RecordingAction("arb")
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=arb_action))
    store.view("match_X").set_persistent("arb_on", True)

    mp = _mp(confidence=1.0)
    # drain 前两次触发(模拟同突发并发):第一次 try_enter 成功派发评估;第二次 gate busy → 不派发
    actor.on_data(mp)
    actor.on_data(mp)
    assert len(loop.tasks) == 1                        # 只派发了一个评估 task(第二个被闸挡)
    _run(_drain(loop))
    assert arb_action.calls == 1                       # 只 fire 一次
    assert gate.is_in_flight("match_X") is True        # fire 后持有,交执行清(本测试无 execution)


# ── eval.16(§6.10 §7):不同 pair 不互相阻塞 ──
def test_different_pairs_not_blocked():
    from src.arbitrage.common.pair_inflight import PairInFlightGate

    gate = PairInFlightGate()
    actor, store, pair_reg, strat_reg, loop, _ = _harness(pair_inflight=gate)
    a1, a2 = _RecordingAction("p1"), _RecordingAction("p2")
    strat_reg.register_pair("P1", _strategy(True, False, arb_action=a1))
    strat_reg.register_pair("P2", _strategy(True, False, arb_action=a2))
    store.view("P1").set_persistent("arb_on", True)
    store.view("P2").set_persistent("arb_on", True)

    actor.on_data(_mp("P1", confidence=1.0))
    actor.on_data(_mp("P2", confidence=1.0))
    assert len(loop.tasks) == 2                        # 不同 pair 各派发
    _run(_drain(loop))
    assert a1.calls == 1 and a2.calls == 1


def test_running_loop_task_dispatch_uses_current_loop():
    """已注册 NT executor 时,当前 loop 内的回调直接在注册 loop 创建 task。"""
    async def scenario():
        actor, store, pair_reg, strat_reg, loop, _ = _harness(log_evaluations=True)
        executor = ThreadPoolExecutor(max_workers=1)
        actor.register_executor(asyncio.get_running_loop(), executor)
        action = _RecordingAction("arb")
        strat_reg.register_pair("match_X", _strategy(True, False, arb_action=action))
        store.view("match_X").set_persistent("arb_on", True)

        try:
            actor.on_data(_mp(confidence=1.0))
            for _ in range(5):
                await asyncio.sleep(0)
        finally:
            executor.shutdown(wait=True)

        assert loop.tasks == []
        assert action.calls == 1

    _run(scenario())


def test_registered_executor_loop_used_without_running_loop():
    """NT msgbus 同步回调无 running loop 时,仍投递到 register_executor 注入的 loop。"""
    actor, store, pair_reg, strat_reg, fallback_loop, _ = _harness(log_evaluations=True)
    action = _RecordingAction("arb")
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=action))
    store.view("match_X").set_persistent("arb_on", True)

    nt_loop = asyncio.new_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        actor.register_executor(nt_loop, executor)
        actor.on_data(_mp(confidence=1.0))
        for _ in range(5):
            nt_loop.run_until_complete(asyncio.sleep(0))

        assert fallback_loop.tasks == []
        assert action.calls == 1
    finally:
        executor.shutdown(wait=True)
        nt_loop.close()


# ── eval.17 已删除(#108):strategy⊥健康检查互斥(`_hc_running` + `health_check.*`)退役 ——
# 执行页 reload 已迁 NT reconciliation,competition 页 reload 在另一张页、OE 下单 page.evaluate
# 与焦点无关,不冲突。详见 synchronization §8.6 / refactor #108。
