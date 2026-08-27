"""StrategyEvaluator —— Q21 完整集成:on_data → 查 → live state → 并行 evaluate → 候选选择。

对应用例:strategy-4.framework.eval.{1-5}
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import TestClock
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.market_order_book import MarketOrderBookDeltas
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from src.arbitrage.common.control import SetArbitrageParamsCommand
from src.arbitrage.common.market_books import market_book_topic
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.common.params import ArbitrageParams
from src.arbitrage.matching.events import MatchedPair
from src.arbitrage.strategy.actions.place_bets import PlaceBetsAction
from src.arbitrage.strategy.actor import StrategyEvaluator
from src.arbitrage.strategy.actor import StrategyEvaluatorConfig
from src.arbitrage.strategy.actor import _RuntimeDeps
from src.arbitrage.strategy.bool_expr import AndExpr
from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import AndCheckExpr
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import Condition
from src.arbitrage.strategy.registry import Strategy
from src.arbitrage.strategy.registry import StrategyRegistry


# ── 工具:fake loop / actions / portfolio ────────────────────────
class _FakeTask:
    """最小 task-like(#260):承载 coro + done-callback。

    真 `asyncio.Task` 完成时由 loop 自动触发 callback;fake loop 没有 loop,故由 `_drain`
    await 完 coro 后显式 `_complete(...)`,让 `_on_eval_done` 与生产同形地拿到 result/exception。
    """

    def __init__(self, coro):
        self.coro = coro
        self._callbacks = []
        self._result = None
        self._exc = None

    def add_done_callback(self, cb):
        self._callbacks.append(cb)

    def cancelled(self):
        return False

    def exception(self):
        return self._exc

    def result(self):
        if self._exc is not None:
            raise self._exc
        return self._result

    def _complete(self, result=None, exc=None):
        self._result, self._exc = result, exc
        for cb in self._callbacks:
            cb(self)


class _FakeLoop:
    def __init__(self):
        self.tasks = []
    def create_task(self, coro):
        task = _FakeTask(coro)
        self.tasks.append(task)
        return task


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


class _CaptureSportsStoreAction(Action):
    def __init__(self):
        self.value = None

    async def execute(self, ctx):
        self.value = ctx.sports_store


class _CaptureRuntimeAction(Action):
    def __init__(self):
        self.strategy_id = None
        self.runtime_store = None

    async def execute(self, ctx):
        self.strategy_id = ctx.strategy_id
        self.runtime_store = ctx.runtime_store


class _CaptureEventNameAction(Action):
    def __init__(self):
        self.value = None
        self.calls = 0

    async def execute(self, ctx):
        self.calls += 1
        self.value = ctx.event_name


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


class _ConstantQuery(StateQuery):
    def __init__(self, value: bool):
        self._value = value

    def matches(self, ctx):
        return self._value


class _SetScratchLegsCheck(Check):
    def __init__(self, legs: list[dict]):
        self._legs = legs

    def passes(self, ctx):
        ctx.scratch["legs"] = list(self._legs)
        return True


class _SetCancelRequestCheck(Check):
    def passes(self, ctx):
        ctx.scratch["cancel_pair_orders"] = {"reason": "spread_cancel_recovery"}
        ctx.scratch["legs"] = [{
            "instrument_id": "H.POLYMARKET",
            "venue": "POLYMARKET",
            "price": 0.5,
            "qty": 8.0,
            "share_if_wins": 8.0,
        }]
        return True


def _strategy(arb_hit: bool, comp_hit: bool, arb_action=None, comp_action=None) -> Strategy:
    """构造一个策略,arb/comp 树各一叶子,通过无状态查询控制命中。"""
    arb_tree = Condition(
        self_hits=_ConstantQuery(arb_hit),
        checktion=AndCheckExpr(_StubCheck(True)),
        actions=[arb_action] if arb_action else [],
    )
    comp_tree = Condition(
        self_hits=_ConstantQuery(comp_hit),
        checktion=AndCheckExpr(_StubCheck(True)),
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
    loop = _FakeLoop()

    active_flag = {"v": execution_active}
    deps = _RuntimeDeps(
        pair_registry=pair_reg,
        strategy_registry=strat_reg,
        portfolio=object(),
        is_pair_executing=lambda pid: active_flag["v"],
        loop=loop,
        arbitrage_params=arbitrage_params,
        pair_inflight=pair_inflight,               # §6.10 §7:per-pair 串行闸(默认 None=不串行)
    )
    actor = StrategyEvaluator(
        StrategyEvaluatorConfig(log_evaluations=log_evaluations),
        deps,
    )
    actor.register(
        trader_id=TraderId("T-000"),
        portfolio=portfolio,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
    )
    return actor, None, pair_reg, strat_reg, loop, active_flag


async def _drain(loop, *, expect_exception: bool = False):
    """跑光 fake loop 中所有 task,并按生产同形触发 done-callback(#260)。

    默认异常照旧上抛(否则新引入的错误会被 `_FakeTask` 吞掉而测试仍绿);
    故意制造异常的用例传 `expect_exception=True`,拿回异常列表自行断言。
    """
    raised = []
    while loop.tasks:
        task = loop.tasks.pop(0)
        try:
            result = await task.coro
        except Exception as e:                 # noqa: BLE001 — 复刻 Task 捕获异常的语义
            task._complete(exc=e)              # callback 要能看到 exception(闸据此释放)
            raised.append(e)
            if not expect_exception:
                raise
        else:
            task._complete(result=result)
    return raised


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _mp(
    pair_id: str = "match_X",
    *,
    confidence: float = 0,
    outcomes: list[str] | None = None,
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
        outcomes=list(outcomes or ["yes", "no"]),
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
    mp = _mp()
    actor.on_data(mp)
    _run(_drain(loop))

    assert action.defaults == {"share": 40.0, "max_leg_share": 100.0}


def test_eval_context_receives_strategy_runtime_identity_and_store():
    actor, _, _, strat_reg, loop, _ = _harness()
    action = _CaptureRuntimeAction()
    strategy = _strategy(True, False, arb_action=action)
    strategy.metadata["id"] = "head_rebate"
    strat_reg.register_pair("match_X", strategy)

    actor.on_data(_mp())
    _run(_drain(loop))

    assert action.strategy_id == "head_rebate"
    assert action.runtime_store is actor._runtime_store


def test_evaluator_injects_sports_store_into_eval_context():
    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    action = _CaptureSportsStoreAction()
    arb_tree = Condition(
        self_hits=AndExpr(),
        checktion=AndCheckExpr(_StubCheck(True)),
        actions=[action],
    )
    strat_reg.register_pair(
        "match_X",
        Strategy(
            scope_key="pair:match_X",
            arbitrage_tree=arb_tree,
            compensation_tree=Condition(self_hits=_ConstantQuery(False)),
        ),
    )

    mp = _mp()
    actor.on_data(mp)
    _run(_drain(loop))

    assert action.value is actor._sports_store
    assert action.value is not None


# ── OBD 触发:所有 tradable venue 均可驱动机会评估 ─────────────────────────
def _obd(iid_str: str):
    """最小 OrderBookDeltas 替身:on_order_book_deltas 只读 `.instrument_id`。"""
    return SimpleNamespace(instrument_id=InstrumentId.from_str(iid_str))


def test_obd_from_decimal_venue_triggers_eval():
    """OE/SE(decimal 赔率盘)的 OBD 触发机会评估。"""
    actor, _, pair_reg, strat_reg, loop, _ = _harness()
    pair_reg.register("match_X", ["A.ORBITEXCH", "H.POLYMARKET"])
    actor._top_ask_changed = MagicMock(return_value=True)
    actor._update_trend_price = MagicMock()
    arb_action = _RecordingAction("arb")
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=arb_action))

    actor.on_order_book_deltas(_obd("A.ORBITEXCH"))
    _run(_drain(loop))

    assert arb_action.calls == 1


def test_obd_from_probability_venue_triggers_eval():
    """PM(probability 概率盘)的 OBD 同样触发机会评估。"""
    actor, _, pair_reg, strat_reg, loop, _ = _harness()
    pair_reg.register("match_X", ["A.ORBITEXCH", "H.POLYMARKET"])
    actor._top_ask_changed = MagicMock(return_value=True)
    actor._update_trend_price = MagicMock()
    arb_action = _RecordingAction("arb")
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=arb_action))

    actor.on_order_book_deltas(_obd("H.POLYMARKET"))
    _run(_drain(loop))

    assert arb_action.calls == 1


def test_obd_injects_event_name_into_eval_context():
    actor, _, pair_reg, strat_reg, loop, _ = _harness()
    pair_reg.register("match_X", ["H.POLYMARKET"])
    action = _CaptureEventNameAction()
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=action))
    actor._top_ask_changed = MagicMock(return_value=True)
    actor._update_trend_price = MagicMock()

    actor.on_order_book_deltas(_obd("H.POLYMARKET"))
    _run(_drain(loop))

    assert action.value == "OrderBookDeltas"


def test_market_obd_routes_each_pair_once_and_injects_market_event_name(monkeypatch):
    actor, _, pair_reg, strat_reg, loop, _ = _harness(
        arbitrage_params=ArbitrageParams(evaluate_on_depth_change=True),
    )
    pair_reg.register("match_X", ["Y.POLYMARKET", "N.POLYMARKET"])
    action = _CaptureEventNameAction()
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=action))
    actor._update_trend_price = MagicMock()
    batch = MarketOrderBookDeltas(
        venue=Venue("POLYMARKET"),
        market_id="condition",
        deltas=(_obd("Y.POLYMARKET"), _obd("N.POLYMARKET")),
        ts_event=0,
        ts_init=0,
    )

    actor.on_data(batch)
    _run(_drain(loop))

    assert action.value == "MarketOrderBookDeltas"
    assert action.calls == 1


def test_market_price_memory_handler_runs_before_strategy_route(monkeypatch):
    actor, _, pair_reg, _, _, _ = _harness()
    mp = _market_pair(actor)
    pair_reg.register(mp.pair_id, mp.tradable_instrument_ids)
    monkeypatch.setattr("src.arbitrage.strategy.actor.subscribe_market_book", lambda *a, **k: None)
    actor.on_data(mp)
    subscription = actor._market_obd_subscribed[("POLYMARKET", "0xcond")]
    order = []
    actor._capture_first_price = lambda deltas: order.append("prices")
    actor._update_extreme_prices = lambda deltas: None
    actor._update_trend_price = lambda deltas: None
    actor._route_eval_market = lambda batch: order.append("eval")
    actor.msgbus.subscribe(
        topic=market_book_topic(subscription),
        handler=actor.on_data,
        priority=0,
    )
    batch = MarketOrderBookDeltas(
        venue=Venue("POLYMARKET"),
        market_id="0xcond",
        deltas=(_obd(mp.venue_instrument_ids["POLYMARKET"][0]),),
        ts_event=0,
        ts_init=0,
    )

    actor.msgbus.publish(topic=market_book_topic(subscription), msg=batch)

    assert order == ["prices", "eval"]


def test_depth_only_obd_does_not_trigger_evaluation_by_default():
    actor, _, pair_reg, strat_reg, loop, _ = _harness()
    pair_reg.register("match_X", ["H.POLYMARKET"])
    action = _RecordingAction("arb")
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=action))
    actor._top_ask_changed = MagicMock(return_value=False)
    actor._update_trend_price = MagicMock()

    actor.on_order_book_deltas(_obd("H.POLYMARKET"))
    _run(_drain(loop))

    assert action.calls == 0
    actor._update_trend_price.assert_called_once()


def test_depth_only_obd_triggers_evaluation_when_enabled():
    actor, _, pair_reg, strat_reg, loop, _ = _harness(
        arbitrage_params=ArbitrageParams(evaluate_on_depth_change=True),
    )
    pair_reg.register("match_X", ["H.POLYMARKET"])
    action = _RecordingAction("arb")
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=action))
    actor._top_ask_changed = MagicMock(return_value=False)
    actor._update_trend_price = MagicMock()

    actor.on_order_book_deltas(_obd("H.POLYMARKET"))
    _run(_drain(loop))

    assert action.calls == 1


def test_depth_switch_hot_update_rejects_non_boolean_value():
    actor, *_ = _harness()

    actor._on_set_arbitrage_params_cmd(
        SetArbitrageParamsCommand(evaluate_on_depth_change="false"),
    )

    assert actor._arbitrage_params.evaluate_on_depth_change is False


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
    mp = _mp()
    actor.on_data(mp)
    _run(_drain(loop))

    assert arb_action.calls == 1
    assert comp_action.calls == 1


def test_log_evaluations_enabled_covers_skip_paths():
    """log_evaluations=True 时无策略 / 执行在飞路径仍保持 no-op。"""
    actor, store, pair_reg, strat_reg, loop, active_flag = _harness(
        execution_active=True,
        log_evaluations=True,
    )
    arb_action = _RecordingAction("arb")
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=arb_action))
    actor.on_data(_mp("match_unknown"))
    actor.on_data(_mp())
    _run(_drain(loop))

    assert arb_action.calls == 0
    assert loop.tasks == []


def test_scheduled_log_includes_pair_top_of_book_values():
    books = {
        "Y.POLYMARKET": {"bid": 0.40, "ask": 0.42},
        "N.POLYMARKET": {"bid": 0.55, "ask": 0.57},
    }
    infos = {
        "Y.POLYMARKET": {"claim": "yes"},
        "N.POLYMARKET": {"claim": "no"},
    }
    fake = SimpleNamespace(
        cache=SimpleNamespace(
            instrument=lambda iid: SimpleNamespace(info=infos[str(iid)]),
            order_book=lambda iid: books[str(iid)],
        ),
        _pair_registry=SimpleNamespace(
            instrument_ids_for_pair=lambda pair_id: set(books),
        ),
        _strategy_registry=SimpleNamespace(
            get_for=lambda pair_id, competition, sport: object(),
        ),
        _log_evaluations=True,
        _log=MagicMock(),
        _pair_inflight=SimpleNamespace(try_enter=lambda pair_id: False),
        _eval_tasks_by_pair={},
    )
    fake._pair_order_book_values = lambda pair_id: StrategyEvaluator._pair_order_book_values(
        fake,
        pair_id,
    )

    StrategyEvaluator._dispatch_eval(
        fake,
        "match_X",
        "Tennis",
        "ATP",
        event_name="OrderBookDeltas",
    )

    expected = [
        {
            "venue": "POLYMARKET",
            "outcome": "no",
            "best_bid": 0.55,
            "best_ask": 0.57,
        },
        {
            "venue": "POLYMARKET",
            "outcome": "yes",
            "best_bid": 0.40,
            "best_ask": 0.42,
        },
    ]
    assert fake._pair_order_book_values("match_X") == expected
    scheduled = fake._log.info.call_args_list[0].args[0]
    assert "event=OrderBookDeltas" in scheduled
    assert f"order_books={expected}" in scheduled


# ── eval.4:两树 Action 链独立规划 ──────────────────────────────────
def test_both_hit_runs_both_planning_chains():
    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    arb_action = _RecordingAction("arb")
    comp_action = _RecordingAction("comp")
    strat_reg.register_pair("match_X", _strategy(True, True, arb_action=arb_action, comp_action=comp_action))
    mp = _mp()
    actor.on_data(mp)
    _run(_drain(loop))
    assert arb_action.calls == 1
    assert comp_action.calls == 1


def test_pair_order_canceler_reloads_and_cancels_all_pair_open_orders():
    first = SimpleNamespace(client_order_id="A")
    second = SimpleNamespace(client_order_id="B")
    outside = SimpleNamespace(client_order_id="OUT")
    pair_registry = PairRegistry()
    pair_registry.register("p", ["H.POLYMARKET", "A.ORBITEXCH"])
    orders = {
        "H.POLYMARKET": [first],
        "A.ORBITEXCH": [second],
        "X.SHARPEXCH": [outside],
    }

    def orders_open(*, instrument_id):
        assert isinstance(instrument_id, InstrumentId)
        return orders.get(str(instrument_id), [])

    fake = SimpleNamespace(
        _pair_registry=pair_registry,
        cache=SimpleNamespace(orders_open=orders_open),
        cancel_order=MagicMock(),
    )

    canceler = StrategyEvaluator._make_pair_order_canceler(fake)

    assert canceler("p") == 2
    assert {call.args[0].client_order_id for call in fake.cancel_order.call_args_list} == {"A", "B"}
    params = [call.kwargs["params"]["arb_cancel_opportunity"] for call in fake.cancel_order.call_args_list]
    assert len({item["opportunity_id"] for item in params}) == 1
    assert {item["cancel_key"] for item in params} == {"A", "B"}
    assert all(set(item["expected_cancels"]) == {"A", "B"} for item in params)


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
        self_hits=_ConstantQuery(True),
        checktion=AndCheckExpr(_SetScratchLegsCheck(arb_legs)),
        actions=[arb_action],
    )
    comp_tree = Condition(
        self_hits=_ConstantQuery(True),
        checktion=AndCheckExpr(_SetScratchLegsCheck(comp_legs)),
        actions=[comp_action],
    )
    strat_reg.register_pair(
        "match_X",
        Strategy(scope_key="pair:match_X", arbitrage_tree=arb_tree, compensation_tree=comp_tree),
    )
    mp = _mp()
    actor.on_data(mp)
    _run(_drain(loop))

    assert arb_action.legs == arb_legs
    assert comp_action.calls == 1


# ── eval.5: 补救兜底 — arb 没命中 + comp 命中 → fire comp ─────────
def test_comp_hit_when_arb_miss_fires_comp():
    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    arb_action = _RecordingAction("arb")
    comp_action = _RecordingAction("comp")
    strat_reg.register_pair("match_X", _strategy(False, True, arb_action=arb_action, comp_action=comp_action))
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
    mp = _mp(confidence=1.0)
    actor.on_data(mp)
    _run(_drain(loop))

    assert len(captured) == 1
    ctx = captured[0]
    assert ctx.submitter is not None              # slice 10a:submitter 已注入
    assert callable(ctx.submitter)
    assert callable(ctx.pair_order_canceler)
    assert ctx.positions_digest is not None


def test_submitter_uses_native_strategy_submit_order_path():
    """原生路径必须先写 NT Cache，再把 SubmitOrder 路由到 RiskEngine。"""
    from nautilus_trader.execution.messages import SubmitOrder
    from nautilus_trader.model.identifiers import StrategyId
    from tests.arbitrage.risk._factories import oe_instrument

    actor, *_ = _harness()
    instrument = oe_instrument("ATP-native-submit", "home")
    actor.cache.add_instrument(instrument)
    commands = []
    actor.msgbus.register(endpoint="RiskEngine.execute", handler=commands.append)

    _run(actor._make_submitter()({
        "instrument_id": instrument.id,
        "side": "BUY",
        "qty": 7.0,
        "price": 2.0,
    }))

    assert len(commands) == 1
    command = commands[0]
    assert isinstance(command, SubmitOrder)
    assert command.strategy_id == StrategyId("ARB-EVAL-001")
    assert actor.cache.order(command.order.client_order_id) is command.order
    assert actor.cache.orders_open(instrument_id=instrument.id) == []


def test_submitter_binds_inventory_sell_to_existing_position_id():
    """PM inventory SELL 经 NT 原生 SubmitOrder 绑定要关闭的 Position。"""
    from nautilus_trader.execution.messages import SubmitOrder
    from nautilus_trader.model.identifiers import PositionId
    from tests.arbitrage.risk._factories import pm_instrument

    actor, *_ = _harness()
    instrument = pm_instrument("ATP-native-reduce", "home")
    actor.cache.add_instrument(instrument)
    commands = []
    actor.msgbus.register(endpoint="RiskEngine.execute", handler=commands.append)
    position_id = PositionId(f"{instrument.id}-EXTERNAL")

    _run(actor._make_submitter()({
        "instrument_id": instrument.id,
        "side": "SELL",
        "qty": 5.0,
        "price": 0.8,
        "position_id": position_id,
    }))

    assert len(commands) == 1
    command = commands[0]
    assert isinstance(command, SubmitOrder)
    assert command.position_id == position_id
    assert actor.cache.position_id(command.order.client_order_id) == position_id


# ── 非目标数据类型(无 pair 信息)→ silently no-op ──────────────
def test_non_routable_data_silently_skipped():
    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    actor.on_data(SimpleNamespace())                   # 既无 instrument_id 也非 MatchedPair
    actor.on_data("garbage")
    actor.on_data(None)
    _run(_drain(loop))
    assert loop.tasks == []                            # 全 skip,无 evaluate task


def _market_pair(actor):
    from tests.arbitrage.matching.test_actor import _oe
    from tests.arbitrage.matching.test_actor import _pm

    instruments = [
        _pm("ATP", "A", "B", "home", "h"),
        _pm("ATP", "A", "B", "away", "a"),
        _oe("ATP", "A", "B", "home", 11),
        _oe("ATP", "A", "B", "away", 12),
    ]
    for instrument in instruments:
        actor.cache.add_instrument(instrument)
    ids = [str(instrument.id) for instrument in instruments]
    return _mp(
        tradable_instrument_ids=ids,
        venue_instrument_ids={"POLYMARKET": ids[:2], "ORBITEXCH": ids[2:]},
        order_books_managed=True,
    )


def test_matched_pair_subscribes_market_obd_deduped(monkeypatch):
    """MatchedPair 按 venue market 订阅；同 market 再来不重复订。"""
    actor, *_ = _harness()
    calls = []
    monkeypatch.setattr(
        "src.arbitrage.strategy.actor.subscribe_market_book",
        lambda _actor, subscription, *, managed: calls.append((subscription, managed)),
    )
    mp = _market_pair(actor)
    actor.on_data(mp)
    assert {subscription.key for subscription, _ in calls} == {
        ("POLYMARKET", "0xcond"), ("ORBITEXCH", "1-123"),
    }
    assert all(managed is False for _, managed in calls)
    actor.on_data(mp)                                  # 再来同 pair → 去重,不再订
    assert len(calls) == 2


def test_matched_pair_without_managed_books_creates_strategy_books(monkeypatch):
    """关闭 matching 概率校验时，Strategy 仍以 managed=True 建立 order books。"""
    actor, *_ = _harness()
    calls = []
    monkeypatch.setattr(
        "src.arbitrage.strategy.actor.subscribe_market_book",
        lambda _actor, subscription, *, managed: calls.append((subscription.key, managed)),
    )

    mp = _market_pair(actor)
    mp.order_books_managed = False
    actor.on_data(mp)

    assert all(managed is True for _, managed in calls)


def test_matched_pair_obd_subscription_uses_tradable_ids_not_anchor_ids(monkeypatch):
    """strategy-pmsports-anchor.1:PMSPORTS anchor 不参与 OBD 订阅。"""
    actor, *_ = _harness()
    calls = []
    monkeypatch.setattr(
        "src.arbitrage.strategy.actor.subscribe_market_book",
        lambda _actor, subscription, *, managed: calls.extend(map(str, subscription.instrument_ids)),
    )
    mp = _market_pair(actor)
    mp.anchor_instrument_ids = ["anchor.PMSPORTS"]

    actor.on_data(mp)

    assert set(calls) == set(mp.tradable_instrument_ids)
    assert "anchor.PMSPORTS" not in calls


def test_matched_pair_obd_subscription_consumes_only_main_fields(monkeypatch):
    """venue-registry.10:Strategy 只消费 MatchedPair 主字段。"""
    actor, *_ = _harness()
    calls = []
    monkeypatch.setattr(
        "src.arbitrage.strategy.actor.subscribe_market_book",
        lambda _actor, subscription, *, managed: calls.append(subscription.key),
    )
    mp = _market_pair(actor)

    actor.on_data(mp)

    assert set(calls) == {("POLYMARKET", "0xcond"), ("ORBITEXCH", "1-123")}


# ── eval.15(§6.10 §7,#84):同 pair 并发触发 → per-pair 闸只放一次 fire ──
def test_same_pair_concurrent_eval_fires_once():
    from src.arbitrage.common.pair_inflight import PairInFlightGate

    gate = PairInFlightGate()
    actor, store, pair_reg, strat_reg, loop, _ = _harness(pair_inflight=gate)
    arb_action = _RecordingAction("arb")
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=arb_action))
    mp = _mp(confidence=1.0)
    # drain 前两次触发(模拟同突发并发):第一次 try_enter 成功派发评估;第二次 gate busy → 不派发
    actor.on_data(mp)
    actor.on_data(mp)
    assert len(loop.tasks) == 1                        # 只派发了一个评估 task(第二个被闸挡)
    _run(_drain(loop))
    assert arb_action.calls == 1                       # 只 fire 一次
    # #260:`_RecordingAction` 不提交任何订单 → 所有权未交出 → 闸必须归还。
    # 旧断言是 `is True`(fire 即持有),那正是泄漏本身:action 空转也永久占闸,该 pair 再不被评估。
    assert gate.is_in_flight("match_X") is False


# ── eval.16(§6.10 §7):不同 pair 不互相阻塞 ──
def test_different_pairs_not_blocked():
    from src.arbitrage.common.pair_inflight import PairInFlightGate

    gate = PairInFlightGate()
    actor, store, pair_reg, strat_reg, loop, _ = _harness(pair_inflight=gate)
    a1, a2 = _RecordingAction("p1"), _RecordingAction("p2")
    strat_reg.register_pair("P1", _strategy(True, False, arb_action=a1))
    strat_reg.register_pair("P2", _strategy(True, False, arb_action=a2))

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


# ── #250:PMSPORTS strategy channel 触发(game_id 扇出)─────────────────────
def _sports_update(game_id=888, *, ts=1, live=True, ended=False):
    from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate

    return SportsGameUpdate(
        ts_event=ts, ts_init=ts, game_id=game_id, league="x", home_team="A", away_team="B",
        status="InProgress", score="1-0", period="Q1", elapsed="", live=live, ended=ended,
        finished_ts="",
    )


def test_sports_update_fans_out_to_all_registered_pairs_for_game():
    """strategy-4.sports.2:同 game 注册多 pair(3-way 场景)→ 一次 strategy 事件全部调度。"""
    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    a1, a2 = _RecordingAction("m1"), _RecordingAction("m2")
    strat_reg.register_pair("m1", _strategy(True, False, arb_action=a1))
    strat_reg.register_pair("m2", _strategy(True, False, arb_action=a2))
    pair_reg.register("m1", ["H1.PM", "H2.OE"], game_id=888)
    pair_reg.register("m2", ["A1.PM", "A2.OE"], game_id=888)

    actor.on_data(_sports_update(888))
    _run(_drain(loop))

    assert a1.calls == 1 and a2.calls == 1


def test_sports_update_unregistered_game_is_noop():
    """strategy-4.sports.3:Store 有状态但 PairRegistry 无 pair → 不创建评估 task,不报错。"""
    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    actor.on_data(_sports_update(999))
    assert loop.tasks == []


def test_sports_fanout_respects_pair_inflight_gate():
    """strategy-4.sports.2 补:扇出各 pair 独立受 PairInFlightGate 约束,无 event 级全局锁。"""
    from src.arbitrage.common.pair_inflight import PairInFlightGate

    gate = PairInFlightGate()
    actor, store, pair_reg, strat_reg, loop, _ = _harness(pair_inflight=gate)
    a1, a2 = _RecordingAction("m1"), _RecordingAction("m2")
    strat_reg.register_pair("m1", _strategy(True, False, arb_action=a1))
    strat_reg.register_pair("m2", _strategy(True, False, arb_action=a2))
    pair_reg.register("m1", ["H1.PM"], game_id=888)
    pair_reg.register("m2", ["A1.PM"], game_id=888)

    assert gate.try_enter("m1")            # m1 已在飞
    actor.on_data(_sports_update(888))
    _run(_drain(loop))

    assert a1.calls == 0 and a2.calls == 1


def test_matched_pair_subscribes_per_game_topic_and_routes_events():
    """strategy-4.sports.1:MatchedPair 到达 → 按场订阅;per-game topic 发布经 NT
    路由到 on_data 并触发评估;不再依赖裸 `data.SportsGameUpdate*`。"""
    from nautilus_trader.adapters.polymarket.sports import SPORTS_CHANNEL_PHASE
    from nautilus_trader.adapters.polymarket.sports import sports_data_type

    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    action = _RecordingAction("m1")
    strat_reg.register_pair("m1", _strategy(True, False, arb_action=action))
    pair_reg.register("m1", ["H1.PM"], game_id=888)   # matching 注册先于 MatchedPair 发布

    actor.start()   # FSM → RUNNING(handle_data 门槛)+ on_start
    actor.on_data(_mp("m1", tradable_instrument_ids=["H1.PM"]))   # → 订 game 888(自身也评估一次)
    assert 888 in actor._sports_subscribed
    _run(_drain(loop))
    calls_after_mp = action.calls

    actor.msgbus.publish(
        topic=f"data.{sports_data_type(888, SPORTS_CHANNEL_PHASE).topic}",
        msg=_sports_update(888),
    )
    _run(_drain(loop))

    assert action.calls == calls_after_mp + 1      # sports 事件恰好触发一次评估


def test_ended_releases_sports_and_obd_subscriptions(monkeypatch):
    """strategy-4.sports.6:ended 分发完毕后释放本场全部订阅(sports + 各腿 OBD)→
    与 matching 侧退订汇合归零,触发 NT 收尾 + 内存回收。"""
    actor, store, pair_reg, strat_reg, loop, _ = _harness()
    a1 = _RecordingAction("m1")
    strat_reg.register_pair("m1", _strategy(True, False, arb_action=a1))
    mp = _market_pair(actor)
    mp.pair_id = "m1"
    pair_reg.register("m1", mp.tradable_instrument_ids, game_id=888)
    monkeypatch.setattr("src.arbitrage.strategy.actor.subscribe_market_book", lambda *a, **k: None)
    monkeypatch.setattr("src.arbitrage.strategy.actor.unsubscribe_market_book", lambda *a, **k: None)

    actor.start()
    actor.on_data(mp)
    assert 888 in actor._sports_subscribed
    assert actor._game_market_obd[888] == {
        ("POLYMARKET", "0xcond"), ("ORBITEXCH", "1-123"),
    }
    assert set(actor._market_obd_subscribed) == actor._game_market_obd[888]
    actor._runtime_store.update("head_rebate", "m1", {"standard": 0.2})

    actor.on_data(_sports_update(888, live=False, ended=True))
    _run(_drain(loop))

    assert 888 not in actor._sports_subscribed
    assert 888 not in actor._game_market_obd
    assert actor._market_obd_subscribed == {}
    assert actor._runtime_store.snapshot() == {}    # 同场跨轮策略变量一并释放


# ── PairPriceStore:PM 初始/开赛价格快照──────────────────────────────
def _wire_pair_price_books(actor, pair_reg, *, yes_ask: float, no_ask: float, game_id=888):
    from tests.arbitrage.matching.test_actor import _add_order_book
    from tests.arbitrage.matching.test_actor import _pm

    yes = _pm("EPL", "Arsenal", "Chelsea", "home", f"price-y-{game_id}", claim="yes")
    no = _pm("EPL", "Arsenal", "Chelsea", "home", f"price-n-{game_id}", claim="no")
    actor.cache.add_instrument(yes)
    actor.cache.add_instrument(no)
    _add_order_book(actor.cache, yes.id, yes_ask)
    _add_order_book(actor.cache, no.id, no_ask)
    pair_reg.register("match_X", [str(yes.id), str(no.id)], game_id=game_id)
    actor.on_data(_mp(
        "match_X",
        outcomes=["yes", "no"],
        tradable_instrument_ids=[str(yes.id), str(no.id)],
        venue_instrument_ids={"POLYMARKET": [str(yes.id), str(no.id)]},
    ))
    return yes, no


def test_first_price_captures_when_sports_state_confirmed_pre():
    # 明确 PRE 时采集赛前首价。
    actor, _, pair_reg, _, loop, _ = _harness()
    yes, _ = _wire_pair_price_books(actor, pair_reg, yes_ask=0.44, no_ask=0.56)
    actor._get_sports_store().put(_sports_update(888, live=False, ended=False))  # PRE

    actor.on_order_book_deltas(_obd(str(yes.id)))
    _run(_drain(loop))

    state = actor._get_pair_price_store().get("match_X")
    assert state.first_price == {"yes": 0.44, "no": 0.56}
    assert state.start_price == {"yes": 0.6, "no": 0.6}


def test_first_price_captures_when_sports_state_none():
    # sports_state=None 与 in_game=False 统一视为赛前；firehose 不推 PRE 时仍能采到首价。
    actor, _, pair_reg, _, loop, _ = _harness()
    yes, _ = _wire_pair_price_books(actor, pair_reg, yes_ask=0.44, no_ask=0.56)
    # 不 put 任何 sports 状态 → sports_store.get(888) 返 None

    actor.on_order_book_deltas(_obd(str(yes.id)))
    _run(_drain(loop))

    assert actor._get_pair_price_store().get("match_X").first_price == {"yes": 0.44, "no": 0.56}


def test_first_price_rejects_dirty_sum_and_non_pm_obd():
    actor, _, pair_reg, _, loop, _ = _harness()
    _, _wire_no = _wire_pair_price_books(actor, pair_reg, yes_ask=0.44, no_ask=0.7)
    actor._get_sports_store().put(_sports_update(888, live=False, ended=False))  # PRE:让流程走到 sum 校验

    actor.on_order_book_deltas(_obd("A.ORBITEXCH"))
    actor.on_order_book_deltas(_obd(str(_wire_no.id)))
    _run(_drain(loop))

    assert actor._get_pair_price_store().get("match_X").first_price == {}


def test_first_price_is_not_captured_after_game_is_live():
    actor, _, pair_reg, _, loop, _ = _harness()
    yes, _ = _wire_pair_price_books(actor, pair_reg, yes_ask=0.44, no_ask=0.56)
    actor._get_sports_store().put(_sports_update(888, live=True, ended=False))

    actor.on_order_book_deltas(_obd(str(yes.id)))
    _run(_drain(loop))

    assert actor._get_pair_price_store().get("match_X").first_price == {}


def test_extreme_prices_update_without_first_price_and_require_clean_sum():
    from tests.arbitrage.matching.test_actor import _add_order_book

    actor, _, pair_reg, _, loop, _ = _harness()
    yes, no = _wire_pair_price_books(actor, pair_reg, yes_ask=0.44, no_ask=0.56)
    actor._get_sports_store().put(_sports_update(888, live=True, ended=False))

    actor.on_order_book_deltas(_obd(str(yes.id)))
    _run(_drain(loop))
    state = actor._get_pair_price_store().get("match_X")
    assert state.first_price == {}
    assert state.up_price == {"yes": 0.44, "no": 0.56}
    assert state.down_price == {"yes": 0.44, "no": 0.56}

    _add_order_book(actor.cache, yes.id, 0.7)
    _add_order_book(actor.cache, no.id, 0.3)
    actor.on_order_book_deltas(_obd(str(no.id)))
    _run(_drain(loop))
    state = actor._get_pair_price_store().get("match_X")
    assert state.up_price == {"yes": 0.7, "no": 0.56}
    assert state.down_price == {"yes": 0.44, "no": 0.3}

    _add_order_book(actor.cache, yes.id, 0.9)
    _add_order_book(actor.cache, no.id, 0.9)
    actor.on_order_book_deltas(_obd(str(yes.id)))
    _run(_drain(loop))
    state = actor._get_pair_price_store().get("match_X")
    assert state.up_price == {"yes": 0.7, "no": 0.56}
    assert state.down_price == {"yes": 0.44, "no": 0.3}


def test_start_price_not_captured_without_witnessed_first_price():
    # late-join 护栏:没采到 first_price → 收到 live phase 也不采 start_price,保持默认。
    # 本用例不发送赛前 OBD，因此即使 None 视为赛前也没有见证 first_price。
    actor, _, pair_reg, _, loop, _ = _harness()
    _wire_pair_price_books(actor, pair_reg, yes_ask=0.8, no_ask=0.7)

    actor.on_data(_sports_update(888, live=True, ended=False))
    _run(_drain(loop))

    state = actor._get_pair_price_store().get("match_X")
    assert state.first_price == {}
    assert state.start_price == {"yes": 0.6, "no": 0.6}


def test_start_price_captures_in_play_after_none_state_first_price_witnessed():
    # happy-path:无 sports 状态时按赛前采 first_price → live phase 把当时完整 PM 盘口整组写成
    # start_price(不做概率和校验)，覆盖 firehose 首帧通常即 IN_PLAY 的实盘路径。
    from tests.arbitrage.matching.test_actor import _add_order_book

    actor, _, pair_reg, _, loop, _ = _harness()
    yes, no = _wire_pair_price_books(actor, pair_reg, yes_ask=0.44, no_ask=0.56)
    actor.on_order_book_deltas(_obd(str(yes.id)))     # None 按赛前 → 采 first_price
    _run(_drain(loop))
    assert actor._get_pair_price_store().get("match_X").first_price == {"yes": 0.44, "no": 0.56}

    _add_order_book(actor.cache, yes.id, 0.8)          # 开赛前后盘口移动
    _add_order_book(actor.cache, no.id, 0.7)
    actor.on_data(_sports_update(888, live=True, ended=False))
    _run(_drain(loop))

    state = actor._get_pair_price_store().get("match_X")
    assert state.start_price == {"yes": 0.8, "no": 0.7}


def test_ended_deletes_pair_prices_after_last_evaluation_finishes():
    actor, _, pair_reg, strat_reg, loop, _ = _harness()
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=_RecordingAction("m1")))
    _wire_pair_price_books(actor, pair_reg, yes_ask=0.44, no_ask=0.56)
    _run(_drain(loop))
    assert actor._get_pair_price_store().get("match_X") is not None

    actor.on_data(_sports_update(888, live=False, ended=True))
    assert actor._get_pair_price_store().get("match_X") is not None

    _run(_drain(loop))
    assert actor._get_pair_price_store().get("match_X") is None
    assert 888 not in actor._price_pairs_by_game

# ── #260:pair 闸的唯一出口 = `_on_eval_done`(加锁/释放同层对称)──────
class _RaisingAction(Action):
    async def execute(self, ctx):
        raise RuntimeError("action boom")


def _gate_harness(action):
    from src.arbitrage.common.pair_inflight import PairInFlightGate

    gate = PairInFlightGate()
    actor, store, pair_reg, strat_reg, loop, _ = _harness(pair_inflight=gate)
    strat_reg.register_pair("match_X", _strategy(True, False, arb_action=action))
    return gate, actor, loop


def test_gate_released_even_when_actions_submit():
    """#261:闸只保证"同 pair 不并发评估" → 评估结束**无条件**释放,有没有下单都一样。

    旧行为("已 fire 则持有,交执行释放")需要一个跨组件交接判据,而判据会漏 —— 那正是
    #260 泄漏的根源。全局 ≤1 执行改由 barrier 用派生态保证,见 `test_engine_barrier.py`。
    """
    gate, actor, loop = _gate_harness(_RecordingAction("arb"))

    actor.on_data(_mp(confidence=1.0))
    _run(_drain(loop))

    assert gate.is_in_flight("match_X") is False


def test_gate_released_when_actions_submit_nothing():
    """action 跑了但零提交(上游清空 legs / 内部 abort)→ 闸必须归还。

    #105 ② 的出口枚举缺这一条:既未 fire-abort(finally 不释放),又无 SubmitOrder 到 Risk
    (barrier 不触发)、无 session(exec_finished 不触发)→ 旧代码下该 pair 永久失效。
    """
    gate, actor, loop = _gate_harness(_RecordingAction("arb"))

    actor.on_data(_mp(confidence=1.0))
    _run(_drain(loop))

    assert gate.is_in_flight("match_X") is False


def test_gate_released_when_action_raises():
    """action 链中途抛异常 → task 以 exception 完成 → 闸归还(不是永久占用)。

    注:`_on_eval_done` 里那条 `Strategy evaluate failed` 日志本测试**不断言** —— NT `Logger`
    是只读 Cython 属性、`init_logging` 每进程只能调一次,拦不住;该日志靠代码审查保证。
    这里断言的是可观测的闸行为。
    """
    gate, actor, loop = _gate_harness(_RaisingAction())

    actor.on_data(_mp(confidence=1.0))
    raised = _run(_drain(loop, expect_exception=True))

    assert [type(e).__name__ for e in raised] == ["RuntimeError"]
    assert gate.is_in_flight("match_X") is False


def test_gate_released_when_task_scheduling_fails():
    """`_create_task` 抛(如非 loop 线程调 create_task)→ 协程从未排程,闸安全归还且异常上抛。"""
    import pytest

    gate, actor, loop = _gate_harness(_RecordingAction("arb"))
    loop.create_task = MagicMock(side_effect=RuntimeError("Non-thread-safe operation"))

    with pytest.raises(RuntimeError, match="Non-thread-safe"):
        actor.on_data(_mp(confidence=1.0))

    assert gate.is_in_flight("match_X") is False


def test_released_gate_allows_reevaluation():
    """零提交释放后,同 pair 下一轮能再次被评估(不是永久失效)。"""
    action = _RecordingAction("arb")
    gate, actor, loop = _gate_harness(action)

    actor.on_data(_mp(confidence=1.0))
    _run(_drain(loop))
    actor.on_data(_mp(confidence=1.0))
    _run(_drain(loop))

    assert action.calls == 2
    assert gate.is_in_flight("match_X") is False


# ── 两树计划统一分发:补偿优先 ──────────────────────────────────────

def test_both_plans_select_compensation_without_inheriting_arb_spread():
    actor, _, _, strat_reg, loop, _ = _harness()
    submitted = []

    async def fake_submitter(spec):
        submitted.append(spec)

    actor._make_submitter = lambda: fake_submitter
    legs = [{
        "instrument_id": "H.POLYMARKET",
        "venue": "POLYMARKET",
        "side": "BUY",
        "role": "yes",
        "price": 0.5,
        "qty": 8.0,
    }]
    arb_tree = Condition(
        self_hits=_ConstantQuery(True),
        checktion=AndCheckExpr(_SetScratchLegsCheck(legs)),
        actions=[PlaceBetsAction(spread=0.05)],
    )
    comp_tree = Condition(
        self_hits=_ConstantQuery(True),
        checktion=AndCheckExpr(_SetScratchLegsCheck(legs)),
        actions=[PlaceBetsAction(intent="recovery")],
    )
    strat_reg.register_pair(
        "match_X",
        Strategy(scope_key="pair:match_X", arbitrage_tree=arb_tree, compensation_tree=comp_tree),
    )

    actor.on_data(_mp())
    _run(_drain(loop))

    assert len(submitted) == 1
    assert submitted[0]["intent"] == "recovery"
    assert submitted[0]["price"] == 0.5


def test_spread_cancel_recovery_completes_comp_tree_then_wins_dispatch():
    actor, _, _, strat_reg, loop, _ = _harness()
    submitted = []
    canceled = []

    async def fake_submitter(spec):
        submitted.append(spec)

    actor._make_submitter = lambda: fake_submitter
    actor._make_pair_order_canceler = lambda: (
        lambda pair_id, **kwargs: canceled.append(pair_id) or 2
    )
    arb_tree = Condition(
        self_hits=_ConstantQuery(True),
        checktion=AndCheckExpr(_SetScratchLegsCheck([{
            "instrument_id": "H.POLYMARKET",
            "venue": "POLYMARKET",
            "price": 0.5,
            "qty": 8.0,
        }])),
        actions=[PlaceBetsAction(spread=0.05)],
    )
    comp_tree = Condition(
        self_hits=_ConstantQuery(True),
        checktion=AndCheckExpr(_SetCancelRequestCheck()),
        actions=[PlaceBetsAction(intent="recovery")],
    )
    strat_reg.register_pair(
        "match_X",
        Strategy(scope_key="pair:match_X", arbitrage_tree=arb_tree, compensation_tree=comp_tree),
    )

    actor.on_data(_mp())
    _run(_drain(loop))

    assert submitted == []
    assert canceled == ["match_X"]


def test_comp_hit_without_plan_falls_back_to_arb_plan():
    actor, _, _, strat_reg, loop, _ = _harness()
    submitted = []

    async def fake_submitter(spec):
        submitted.append(spec)

    actor._make_submitter = lambda: fake_submitter
    arb_tree = Condition(
        self_hits=_ConstantQuery(True),
        checktion=AndCheckExpr(_SetScratchLegsCheck([{
            "instrument_id": "H.POLYMARKET",
            "venue": "POLYMARKET",
            "price": 0.5,
            "qty": 8.0,
        }])),
        actions=[PlaceBetsAction()],
    )
    comp_tree = Condition(
        self_hits=_ConstantQuery(True),
        checktion=AndCheckExpr(_StubCheck(True)),
        actions=[_RecordingAction("comp")],
    )
    strat_reg.register_pair(
        "match_X",
        Strategy(scope_key="pair:match_X", arbitrage_tree=arb_tree, compensation_tree=comp_tree),
    )

    actor.on_data(_mp())
    _run(_drain(loop))

    assert len(submitted) == 1
    assert submitted[0]["intent"] == "arbitrage"
