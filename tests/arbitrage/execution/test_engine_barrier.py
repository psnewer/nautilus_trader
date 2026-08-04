"""ArbLiveExecutionEngine opportunity barrier 单测。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.live.execution_engine import LiveExecutionEngine
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Quantity
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.test_kit.mocks.exec_clients import MockExecutionClient
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.trading.strategy import Strategy
from src.arbitrage.common.opportunity import RISK_LEG_DENIED_TOPIC
from src.arbitrage.common.opportunity import CancelOpportunityMeta
from src.arbitrage.common.opportunity import OpportunityMeta
from src.arbitrage.common.opportunity import cancel_params_from_meta
from src.arbitrage.common.positions import pair_positions_digest
from src.arbitrage.common.realized_pnl import RealizedPnlLedger
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.execution.engine import ArbLiveExecutionEngine
from src.arbitrage.execution.engine import _CommandGroupContext
from src.arbitrage.execution.reconciliation import GuardedReports
from src.arbitrage.execution.reconciliation import ReconciliationStateSnapshot
from tests.arbitrage.risk._factories import pm_instrument


class _RecordingExecutionClient(MockExecutionClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.residual_cancels = []
        self.executing_pairs: set[str] = set()   # #316:模拟归属各 pair 的在飞 session

    def _cancel_residual_orders(self, instrument_id, residual: list) -> None:
        self.residual_cancels.append((instrument_id, list(residual)))

    def _pair_execution_active(self, pair_id, resolve_pair) -> bool:
        # #316:barrier 的 session 源(per-pair);测试直接设 `executing_pairs`,不走 registry 兜底。
        return pair_id in self.executing_pairs


class _Ctx:
    def __init__(self) -> None:
        self.clock = LiveClock()
        self.trader_id = TraderId("TESTER-000")
        self.loop = asyncio.new_event_loop()
        self.msgbus = MessageBus(trader_id=self.trader_id, clock=self.clock)
        self.cache = TestComponentStubs.cache()
        self.instrument = pm_instrument("match_X", "home")
        self.cache.add_instrument(self.instrument)
        self.client = _RecordingExecutionClient(
            client_id=ClientId("POLYMARKET"),
            venue=Venue("POLYMARKET"),
            account_type=AccountType.CASH,
            base_currency=None,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )
        self.engine = ArbLiveExecutionEngine(
            loop=self.loop,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            config=LiveExecEngineConfig(),
        )
        self.engine.register_client(self.client)
        self.liveness = VenueExecutionLiveness()
        self.engine.configure_arb(venue_liveness=self.liveness)
        self.portfolio = Portfolio(msgbus=self.msgbus, cache=self.cache, clock=self.clock)
        self.strategy = Strategy()
        self.strategy.register(
            trader_id=self.trader_id,
            portfolio=self.portfolio,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )

    def submit_cmd(
        self,
        leg_key: str,
        expected=("pm:home:0", "oe:away:1"),
        intent: str = "arbitrage",
        opportunity_id: str = "opp-1",
        pair_id: str = "pair-1",
        positions_digest: str | None = None,
    ) -> SubmitOrder:
        positions_baseline = positions_digest or pair_positions_digest(
            self.cache,
            [self.instrument.id],
        )
        order = self.strategy.order_factory.limit(
            self.instrument.id,
            OrderSide.BUY,
            Quantity.from_int(10),
            self.instrument.make_price(0.4),
            tags=[
                f"arb:opportunity_id={opportunity_id}",
                f"arb:pair_id={pair_id}",
                f"arb:leg_key={leg_key}",
                f"arb:expected_legs={','.join(expected)}",
                f"arb:intent={intent}",
                f"arb:positions_digest={positions_baseline}",
            ],
        )
        return SubmitOrder(
            trader_id=self.trader_id,
            strategy_id=self.strategy.id,
            position_id=None,
            order=order,
            command_id=UUID4(),
            ts_init=self.clock.timestamp_ns(),
        )

    def cancel_cmd(
        self,
        cancel_key: str,
        expected=("cancel-a", "cancel-b"),
        opportunity_id: str = "cancel-opp-1",
        pair_id: str = "pair-1",
    ) -> CancelOrder:
        return CancelOrder(
            trader_id=self.trader_id,
            strategy_id=self.strategy.id,
            instrument_id=self.instrument.id,
            client_order_id=ClientOrderId(cancel_key),
            venue_order_id=None,
            command_id=UUID4(),
            ts_init=self.clock.timestamp_ns(),
            params=cancel_params_from_meta(
                CancelOpportunityMeta(
                    opportunity_id=opportunity_id,
                    pair_id=pair_id,
                    cancel_key=cancel_key,
                    expected_cancels=tuple(expected),
                ),
            ),
        )


def test_barrier_waits_until_all_legs_pass_before_release():
    ctx = _Ctx()
    first = ctx.submit_cmd("pm:home:0")
    second = ctx.submit_cmd("oe:away:1")

    ctx.engine._execute_command(first)
    assert len(ctx.client.commands) == 0

    ctx.engine._execute_command(second)
    assert len(ctx.client.commands) == 2


def test_cancel_barrier_waits_until_all_commands_before_release():
    ctx = _Ctx()
    first = ctx.cancel_cmd("cancel-a")
    second = ctx.cancel_cmd("cancel-b")

    ctx.engine._execute_command(first)
    assert len(ctx.client.commands) == 0

    ctx.engine._execute_command(second)
    assert ctx.client.commands == [first, second]


def test_cancel_barrier_rejects_group_when_other_execution_is_active(monkeypatch):
    ctx = _Ctx()
    ctx.client.executing_pairs = {"pair-1"}   # #316:同 pair(cancel 默认 pair-1)有执行在飞 → 拒
    rejected = []
    monkeypatch.setattr(
        ctx.engine,
        "_reject_cancel",
        lambda command, reason: rejected.append((command.client_order_id.value, reason)),
    )

    ctx.engine._execute_command(ctx.cancel_cmd("cancel-a"))
    ctx.engine._execute_command(ctx.cancel_cmd("cancel-b"))

    assert [item[0] for item in rejected] == ["cancel-a", "cancel-b"]
    assert len(ctx.client.commands) == 0


def test_cancel_group_is_blocked_by_pending_submit_group(monkeypatch):
    ctx = _Ctx()
    rejected = []
    monkeypatch.setattr(
        ctx.engine,
        "_reject_cancel",
        lambda command, reason: rejected.append((command.client_order_id.value, reason)),
    )
    ctx.engine._execute_command(ctx.submit_cmd("pm:home:0"))

    ctx.engine._execute_command(ctx.cancel_cmd("cancel-a"))
    ctx.engine._execute_command(ctx.cancel_cmd("cancel-b"))

    assert [item[0] for item in rejected] == ["cancel-a", "cancel-b"]
    assert ("submit", "opp-1") in ctx.engine._arb_command_groups
    assert ("cancel", "cancel-opp-1") not in ctx.engine._arb_command_groups


def test_cancel_barrier_timeout_rejects_arrived_commands(monkeypatch):
    ctx = _Ctx()
    rejected = []
    monkeypatch.setattr(
        ctx.engine,
        "_reject_cancel",
        lambda command, reason: rejected.append((command.client_order_id.value, reason)),
    )
    ctx.engine._execute_command(ctx.cancel_cmd("cancel-a"))

    timer = SimpleNamespace(name="arb_group_timeout:cancel:cancel-opp-1")
    ctx.engine._on_group_timeout(timer)

    assert rejected == [("cancel-a", "cancel opportunity barrier timeout")]
    assert ("cancel", "cancel-opp-1") not in ctx.engine._arb_command_groups


def test_barrier_releases_when_positions_unchanged_ignoring_open_order_state():
    """#317:barrier 只校验 position 基线;position 不变即 release,open-order 集变化(如窗口内
    正常撤单,改 order 集、不动 position)不再误拒。submit_cmd 用真实 position 基线,release 重算一致。"""
    ctx = _Ctx()
    first = ctx.submit_cmd("pm:home:0")
    second = ctx.submit_cmd("oe:away:1")

    ctx.engine._execute_command(first)
    ctx.engine._execute_command(second)

    assert len(ctx.client.commands) == 2                 # 两腿都 release,无 order-digest 拦截


def test_barrier_denies_all_legs_when_pair_positions_changed(monkeypatch):
    ctx = _Ctx()
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda event: denied.append(event))
    first = ctx.submit_cmd("pm:home:0", positions_digest="baseline")
    second = ctx.submit_cmd("oe:away:1", positions_digest="baseline")
    monkeypatch.setattr(
        "src.arbitrage.execution.engine.pair_positions_digest",
        lambda cache, instrument_ids: "changed",
    )

    ctx.engine._execute_command(first)
    ctx.engine._execute_command(second)

    assert len(ctx.client.commands) == 0
    assert len(_denied_reasons(denied)) == 2
    assert ("submit", "opp-1") not in ctx.engine._arb_command_groups


def test_barrier_denies_legacy_opportunity_without_positions_baseline():
    ctx = _Ctx()
    first = ctx.submit_cmd("pm:home:0")
    second = ctx.submit_cmd("oe:away:1")
    first.order.tags[:] = [tag for tag in first.order.tags if "positions_digest" not in tag]
    second.order.tags[:] = [tag for tag in second.order.tags if "positions_digest" not in tag]

    ctx.engine._execute_command(first)
    ctx.engine._execute_command(second)

    assert len(ctx.client.commands) == 0


def test_barrier_denies_opportunity_when_leg_positions_digests_differ():
    ctx = _Ctx()
    first = ctx.submit_cmd("pm:home:0", positions_digest="first")
    second = ctx.submit_cmd("oe:away:1", positions_digest="second")

    ctx.engine._execute_command(first)
    ctx.engine._execute_command(second)

    assert len(ctx.client.commands) == 0


def test_barrier_deny_blocks_pending_leg():
    ctx = _Ctx()
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda event: denied.append(event))
    first = ctx.submit_cmd("pm:home:0")

    ctx.engine._execute_command(first)
    ctx.msgbus.publish(topic=RISK_LEG_DENIED_TOPIC, msg={
        "opportunity_id": "opp-1",
        "pair_id": "pair-1",
        "leg_key": "oe:away:1",
        "client_order_id": "x",
        "reason": "risk blocked",
    })

    assert len(ctx.client.commands) == 0
    assert denied


def test_barrier_timeout_blocks_pending_leg():
    ctx = _Ctx()
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda event: denied.append(event))
    first = ctx.submit_cmd("pm:home:0")

    ctx.engine._execute_command(first)
    ctx.engine._on_group_timeout(
        type("Evt", (), {"name": "arb_group_timeout:submit:opp-1"})(),
    )

    assert len(ctx.client.commands) == 0
    assert denied


def test_barrier_cancel_only_blocks_all_new_submits_when_residual_exists():
    ctx = _Ctx()
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda event: denied.append(event))
    first = ctx.submit_cmd("pm:home:0")
    second = ctx.submit_cmd("oe:away:1")
    residual = ctx.submit_cmd("pm:home:0").order
    ctx.engine._opportunity_residuals = (
        lambda barrier_ctx: [(ctx.client, ctx.instrument.id, [residual])]
    )

    ctx.engine._execute_command(first)
    ctx.engine._execute_command(second)

    assert len(ctx.client.commands) == 0
    assert ctx.client.residual_cancels == [(ctx.instrument.id, [residual])]
    assert len(denied) == 2


def test_barrier_cancel_only_fires_even_with_execution_session_in_flight():
    """多腿机会:准入后中途冒出在飞执行/撤单 session,cancel-only 动作仍照常触发(不被 ≤1 派生态阻塞)。

    准入闸只在 ctx 创建(首腿)那一刻查同 pair 执行在飞,故 session 必须在首腿之后出现才落到本场景;
    末腿到达时 ctx 已存在、不再查闸,`_release`→`_cancel_only` 也全程不看执行态。
    """
    ctx = _Ctx()
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda event: denied.append(event))
    first = ctx.submit_cmd("pm:home:0")
    second = ctx.submit_cmd("oe:away:1")
    residual = ctx.submit_cmd("pm:home:0").order
    ctx.engine._opportunity_residuals = (
        lambda barrier_ctx: [(ctx.client, ctx.instrument.id, [residual])]
    )

    ctx.engine._execute_command(first)            # 首腿:准入闸 False(无 session)→ ctx 建立
    ctx.client.executing_pairs.add("pair-1")       # 中途:同 pair 撤单/执行 session 冒出(多腿窗口)
    ctx.engine._execute_command(second)            # 末腿:ctx 已存在、不再查闸 → _release → cancel-only

    # cancel-only 不被在飞 session 阻塞:残单撤单照常发起(且新腿仍被拒,不是放行)
    assert ctx.client.residual_cancels == [(ctx.instrument.id, [residual])]
    assert len(ctx.client.commands) == 0
    assert len(denied) == 2


def test_barrier_residual_check_is_pair_wide_even_for_single_leg_opportunity():
    ctx = _Ctx()
    allowed = ctx.submit_cmd("pm:away:0", expected=("pm:away:0",))
    residual_instrument = pm_instrument("match_X", "home", token="tok-home-residual")
    residual = ctx.strategy.order_factory.limit(
        residual_instrument.id,
        OrderSide.BUY,
        Quantity.from_int(10),
        residual_instrument.make_price(0.4),
    )

    class _FakePairRegistry:
        def instrument_ids_for_pair(self, pair_id):
            assert pair_id == "pair-1"
            return {str(allowed.order.instrument_id), str(residual_instrument.id)}

    class _FakeCache:
        def orders_open(self, instrument_id=None):
            return [residual] if str(instrument_id) == str(residual_instrument.id) else []

    fake_engine = type("FakeEngine", (), {})()
    fake_engine._pair_registry = _FakePairRegistry()
    fake_engine._cache = _FakeCache()
    fake_engine._log = ctx.engine._log
    fake_engine._residual_check_instrument_ids = (
        lambda barrier_ctx: ArbLiveExecutionEngine._residual_check_instrument_ids(fake_engine, barrier_ctx)
    )
    fake_engine._client_for_instrument = lambda instrument_id: ctx.client
    fake_engine._find_client_for_command = lambda command: ctx.client
    barrier_ctx = _CommandGroupContext(
        kind="submit",
        group_id="opp-1",
        pair_id="pair-1",
        meta=OpportunityMeta(
            opportunity_id="opp-1",
            pair_id="pair-1",
            leg_key="pm:away:0",
            expected_legs=("pm:away:0",),
        ),
        expected={"pm:away:0"},
        commands={"pm:away:0": allowed},
    )

    residuals = ArbLiveExecutionEngine._opportunity_residuals(fake_engine, barrier_ctx)

    assert residuals == [(ctx.client, residual_instrument.id, [residual])]


# ── #261:barrier 单点 + 纯派生态(#316 起 per-pair ≤1 执行)──────────────
def _denied_reasons(events):
    return [getattr(e, "reason", "") for e in events if type(e).__name__ == "OrderDenied"]


def test_second_opportunity_denied_while_first_waits_in_barrier():
    """派生源 ①:barrier 里还有**同 pair** 非墓碑 ctx → 新同 pair 机会丢弃;跨 pair 放行(#316)。"""
    ctx = _Ctx()
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda e: denied.append(e))

    ctx.engine._execute_command(ctx.submit_cmd("pm:home:0"))          # opp-1(pair-1)第一腿,等第二腿

    # 同 pair(pair-1)新机会 → 拒
    ctx.engine._execute_command(
        ctx.submit_cmd("pm:home:0", opportunity_id="opp-2", pair_id="pair-1"),
    )
    assert ctx.engine._arb_command_groups[("submit", "opp-2")].terminal == "denied"
    assert len(ctx.client.commands) == 0                              # 谁都没下到 venue
    assert any("another opportunity" in r or "denied" in r for r in _denied_reasons(denied))

    # 跨 pair(pair-2)新机会 → 放行(非墓碑,等第二腿)
    ctx.engine._execute_command(
        ctx.submit_cmd("pm:home:0", opportunity_id="opp-3", pair_id="pair-2"),
    )
    assert ctx.engine._arb_command_groups[("submit", "opp-3")].terminal is None


def test_same_pair_opportunity_denied_but_cross_pair_allowed_while_session_live():
    """#316 per-pair ≤1:同 pair 有 session 在飞 → 新同 pair 机会丢弃;跨 pair 机会放行。

    #254 的 PM accepted→fill 无预扣窗口原靠"全局 ≤1 执行"兜底;#264 起 PM calculated account
    在 open/accepted 单上 native locking 占用余额,该窗口不再依赖全局 ≤1(见 refactor.md #316/#254),
    故执行槽收窄为 per-pair、跨 pair 并发放行。
    """
    ctx = _Ctx()
    ctx.client.executing_pairs = {"pair-1"}                           # pair-1 有 session 在飞
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda e: denied.append(e))

    # 同 pair(pair-1)新机会 → 拒(墓碑)
    ctx.engine._execute_command(
        ctx.submit_cmd("pm:home:0", opportunity_id="opp-2", pair_id="pair-1"),
    )
    assert ctx.engine._arb_command_groups[("submit", "opp-2")].terminal == "denied"
    assert len(ctx.client.commands) == 0
    assert _denied_reasons(denied)

    # 跨 pair(pair-2)新机会 → 放行(ctx 建立、非墓碑,等第二腿)
    ctx.engine._execute_command(
        ctx.submit_cmd("pm:home:0", opportunity_id="opp-3", pair_id="pair-2"),
    )
    assert ctx.engine._arb_command_groups[("submit", "opp-3")].terminal is None


def test_tombstone_denies_late_legs_and_pops_when_complete():
    """墓碑:被拒机会的后到腿立刻被拒;`denied` 集齐即提前 pop,不空等 barrier timer。

    没有墓碑的话会出现「B1 被拒 → A 执行结束 → B2 另建 ctx 空等 2s」,那个 ctx 期间
    会挡住合法新机会。
    """
    ctx = _Ctx()
    ctx.client.executing_pairs = {"pair-1"}
    ctx.engine._execute_command(ctx.submit_cmd("pm:home:0", opportunity_id="opp-2"))
    assert ctx.engine._arb_command_groups[("submit", "opp-2")].terminal == "denied"

    ctx.client.executing_pairs.clear()                               # 执行结束
    ctx.engine._execute_command(ctx.submit_cmd("oe:away:1", opportunity_id="opp-2"))

    # 后到腿没有另建 ctx、没有被放行;墓碑已随 denied 集齐被清掉
    assert len(ctx.client.commands) == 0
    assert ("submit", "opp-2") not in ctx.engine._arb_command_groups


def test_tombstone_does_not_block_a_legitimate_new_opportunity():
    """墓碑自身必须被 `_other_execution_in_flight` 跳过,否则它会挡住别人。"""
    ctx = _Ctx()
    ctx.client.executing_pairs = {"pair-1"}
    ctx.engine._execute_command(ctx.submit_cmd("pm:home:0", opportunity_id="opp-2"))
    assert ctx.engine._arb_command_groups[("submit", "opp-2")].terminal == "denied"

    ctx.client.executing_pairs.clear()
    assert ctx.engine._other_execution_in_flight_for_pair("pair-1") is False   # 墓碑不算在飞


def test_tombstone_is_reclaimed_by_barrier_timer_when_legs_never_complete():
    """结构保证:某腿根本没发出(如 submitter 的 instrument 缺失)→ `denied` 永远凑不齐,
    只能靠 barrier timer 回收。提前 pop 是路径,timer 是保证,不能只留路径。"""
    ctx = _Ctx()
    ctx.client.executing_pairs = {"pair-1"}
    ctx.engine._execute_command(ctx.submit_cmd("pm:home:0", opportunity_id="opp-2"))
    assert ("submit", "opp-2") in ctx.engine._arb_command_groups

    ctx.engine._on_group_timeout(
        type("Evt", (), {"name": "arb_group_timeout:submit:opp-2"})(),
    )
    assert ("submit", "opp-2") not in ctx.engine._arb_command_groups


def test_new_opportunity_allowed_once_nothing_in_flight():
    """闸不是单向的:执行结束后新机会必须能正常通过(验非空断言)。"""
    ctx = _Ctx()
    ctx.client.executing_pairs = {"pair-1"}
    ctx.engine._execute_command(ctx.submit_cmd("pm:home:0", opportunity_id="opp-2"))
    assert len(ctx.client.commands) == 0

    ctx.client.executing_pairs.clear()
    ctx.engine._execute_command(ctx.submit_cmd("pm:home:0", opportunity_id="opp-3"))
    ctx.engine._execute_command(ctx.submit_cmd("oe:away:1", opportunity_id="opp-3"))
    assert len(ctx.client.commands) == 2                                # 两腿都下到 venue


# ── #263:leg_denied 早于 sibling ctx 的竞态(拒单通知先到)──────────────
def test_leg_denied_before_sibling_ctx_leaves_no_orphan():
    """Risk 队列发的 leg_denied 早于 Exec 队列建 ctx → 建持久墓碑,sibling 腿命中被拒,不成孤儿。

    #263 前:`ctx is None → return` 丢弃拒单;sibling 腿随后建 ctx 成孤儿,占住全局执行槽
    直到 barrier 超时(#261 后阻断所有机会 → deny 风暴)。这里验证 sibling 腿到达时被立即拒、
    且机会即时清出 `_arb_command_groups`(denied 集齐),不占槽。
    """
    ctx = _Ctx()
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda event: denied.append(event))

    # 拒单通知先到(sibling 的 ctx 还没建),带 expected_legs
    ctx.msgbus.publish(topic=RISK_LEG_DENIED_TOPIC, msg={
        "opportunity_id": "opp-1",
        "pair_id": "pair-1",
        "leg_key": "oe:away:1",
        "expected_legs": ["pm:home:0", "oe:away:1"],
        "client_order_id": "x",
        "reason": "NOTIONAL_LESS_THAN_MIN",
    })
    # 墓碑已建、在等 sibling,但对全局闸不算"在执行"
    assert ("submit", "opp-1") in ctx.engine._arb_command_groups
    assert ctx.engine._other_execution_in_flight_for_pair("pair-1") is False

    # sibling(过 Risk 的那条腿)随后到 barrier
    sibling = ctx.submit_cmd("pm:home:0")
    ctx.engine._execute_command(sibling)

    assert len(ctx.client.commands) == 0                 # sibling 没下到 venue
    assert denied                                        # sibling 被拒
    assert ("submit", "opp-1") not in ctx.engine._arb_command_groups


def test_leg_denied_tombstone_reclaimed_by_timer_when_sibling_never_arrives():
    """结构兜底:sibling 腿始终不来(如它也在 Risk 被拒但通知丢失)→ barrier timer 回收墓碑。"""
    ctx = _Ctx()
    ctx.msgbus.publish(topic=RISK_LEG_DENIED_TOPIC, msg={
        "opportunity_id": "opp-1",
        "pair_id": "pair-1",
        "leg_key": "oe:away:1",
        "expected_legs": ["pm:home:0", "oe:away:1"],   # 两腿,只来了一条拒单
        "client_order_id": "x",
        "reason": "risk blocked",
    })
    assert ("submit", "opp-1") in ctx.engine._arb_command_groups

    ctx.engine._on_group_timeout(
        type("Evt", (), {"name": "arb_group_timeout:submit:opp-1"})(),
    )
    assert ("submit", "opp-1") not in ctx.engine._arb_command_groups


def test_leg_denied_without_expected_legs_cleans_immediately():
    """expected_legs 缺失(非 arb / 旧格式)→ 退化为无竞态保护:立即清,不留墓碑。"""
    ctx = _Ctx()
    ctx.msgbus.publish(topic=RISK_LEG_DENIED_TOPIC, msg={
        "opportunity_id": "opp-1",
        "pair_id": "pair-1",
        "leg_key": "oe:away:1",
        "client_order_id": "x",
        "reason": "risk blocked",
    })
    assert ("submit", "opp-1") not in ctx.engine._arb_command_groups


def test_stale_order_report_batch_is_discarded_at_engine_boundary(monkeypatch):
    ctx = _Ctx()
    command = ctx.submit_cmd("pm:home:0", expected=("pm:home:0",))
    command.order.apply(TestEventStubs.order_submitted(command.order, ctx.client.account_id))
    ctx.cache.add_order(command.order)
    snapshot = SimpleNamespace(is_current_for_instruments=lambda client, instrument_ids: False)

    async def fake_query(_command):
        return GuardedReports([], snapshot=snapshot)

    ctx.client.generate_order_status_reports = fake_query
    reports, venue_reported_ids = ctx.loop.run_until_complete(
        ctx.engine._query_order_status_reports(),
    )

    assert reports == []
    assert venue_reported_ids == {command.order.client_order_id}
    assert ctx.liveness.order_alive(ctx.client.venue)


def test_stale_position_report_batch_is_failed_at_engine_boundary(monkeypatch):
    ctx = _Ctx()
    snapshot = SimpleNamespace(is_current_for_instruments=lambda client, instrument_ids: False)
    report = SimpleNamespace(account_id=ctx.client.account_id, instrument_id=ctx.instrument.id)

    async def fake_query(_command):
        return GuardedReports([report], snapshot=snapshot)

    ctx.client.generate_position_status_reports = fake_query
    venue_positions, failed_venues = ctx.loop.run_until_complete(
        ctx.engine._query_position_status_reports(),
    )

    assert venue_positions == {}
    assert failed_venues == {ctx.client.venue}
    assert ctx.liveness.position_alive(ctx.client.venue)


def test_periodic_order_query_exception_marks_only_order_dead():
    ctx = _Ctx()
    ctx.liveness.mark_order_alive(ctx.client.venue)
    ctx.liveness.mark_position_alive(ctx.client.venue)

    async def fake_query(_command):
        raise RuntimeError("order unavailable")

    ctx.client.generate_order_status_reports = fake_query
    reports, venue_reported_ids = ctx.loop.run_until_complete(
        ctx.engine._query_order_status_reports(),
    )

    assert reports == []
    assert venue_reported_ids == set()
    assert not ctx.liveness.order_alive(ctx.client.venue)
    assert ctx.liveness.position_alive(ctx.client.venue)


def test_periodic_position_query_exception_marks_only_position_dead():
    ctx = _Ctx()
    ctx.liveness.mark_order_alive(ctx.client.venue)
    ctx.liveness.mark_position_alive(ctx.client.venue)

    async def fake_query(_command):
        raise RuntimeError("position unavailable")

    ctx.client.generate_position_status_reports = fake_query
    venue_positions, failed_venues = ctx.loop.run_until_complete(
        ctx.engine._query_position_status_reports(),
    )

    assert venue_positions == {}
    assert failed_venues == {ctx.client.venue}
    assert ctx.liveness.order_alive(ctx.client.venue)
    assert not ctx.liveness.position_alive(ctx.client.venue)


def test_mass_status_delegates_to_super_with_per_pair_guard(monkeypatch):
    """#318:启动 mass-status 不再整体 abort;逐 report 的 per-pair 判定交给 super 里的
    `_reconciliation_report_is_current`。super 被 mock,验证委托发生(不 all-or-nothing)。"""
    ctx = _Ctx()
    snapshot = SimpleNamespace(is_current_for_instruments=lambda client, instrument_ids: False)
    parent_calls = []

    def fake_reconcile(_engine, _mass_status):
        parent_calls.append(True)
        return True

    monkeypatch.setattr(LiveExecutionEngine, "_reconcile_execution_mass_status", fake_reconcile)
    mass_status = SimpleNamespace(
        client_id=ctx.client.id,
        _arb_reconciliation_batches={
            "position": GuardedReports([], snapshot=snapshot),
        },
    )
    result = ctx.engine._reconcile_execution_mass_status(mass_status)

    assert result is True
    assert parent_calls == [True]


def test_valid_position_report_batch_commits_deferred_payload():
    ctx = _Ctx()
    snapshot = SimpleNamespace(is_current_for_instruments=lambda client, instrument_ids: True)
    applied = []

    async def fake_query(_command):
        return GuardedReports([], snapshot=snapshot, payload={"instrument": 1.25})

    ctx.client.generate_position_status_reports = fake_query
    ctx.client.apply_reconciliation_batch = lambda kind, batch, applied_instruments: applied.append(
        (kind, batch.payload),
    )
    venue_positions, failed_venues = ctx.loop.run_until_complete(
        ctx.engine._query_position_status_reports(),
    )

    assert venue_positions == {}
    assert failed_venues == set()
    assert applied == [("position", {"instrument": 1.25})]


def test_valid_position_batch_selective_offset_and_fresh_snapshot():
    """#318:position 批通过后 realized offset 按通过的 instrument 选择性 commit,且给通过的 report
    附上重新捕获的 fresh 真快照(供 NT 逐 report 应用前 per-pair 校验)。"""
    ctx = _Ctx()
    ledger = RealizedPnlLedger()
    ctx.client._realized_pnl_ledger = ledger
    report = SimpleNamespace(instrument_id=ctx.instrument.id)
    snapshot = SimpleNamespace(
        is_current_for_instruments=lambda client, instrument_ids: True,
        kind="position",
    )
    applied = []

    async def fake_query(_command):
        return GuardedReports([report], snapshot=snapshot, payload={"instrument": 1.25})

    def apply_batch(kind, batch, applied_instruments):
        applied.append((kind, set(applied_instruments)))

    ctx.client.generate_position_status_reports = fake_query
    ctx.client.apply_reconciliation_batch = apply_batch

    venue_positions, failed_venues = ctx.loop.run_until_complete(
        ctx.engine._query_position_status_reports(),
    )

    assert venue_positions == {ctx.instrument.id: report}
    assert failed_venues == set()
    assert applied == [("position", {str(ctx.instrument.id)})]
    assert isinstance(report._arb_reconciliation_snapshot, ReconciliationStateSnapshot)
    assert report._arb_reconciliation_snapshot.kind == "position"


def test_stale_single_order_report_is_discarded_at_apply_boundary(monkeypatch):
    ctx = _Ctx()
    parent_calls = []

    def fake_reconcile(_engine, _report, _trades, _is_external=True):
        parent_calls.append(True)
        return True

    monkeypatch.setattr(LiveExecutionEngine, "_reconcile_order_report", fake_reconcile)
    report = SimpleNamespace(
        id="report-1",
        account_id=ctx.client.account_id,
        instrument_id=ctx.instrument.id,
        _arb_reconciliation_snapshot=SimpleNamespace(is_current_for_instruments=lambda client, instrument_ids: False),
    )

    assert ctx.engine._reconcile_order_report(report, []) is False
    assert parent_calls == []


def test_flat_position_report_inherits_empty_batch_snapshot(monkeypatch):
    ctx = _Ctx()
    parent_calls = []

    def fake_reconcile(_engine, _report):
        parent_calls.append(True)
        return True

    monkeypatch.setattr(LiveExecutionEngine, "_reconcile_position_report", fake_reconcile)
    snapshot = SimpleNamespace(is_current_for_instruments=lambda client, instrument_ids: False)
    ctx.engine._arb_position_reconciliation_snapshots[ctx.instrument.id.venue] = snapshot

    report = ctx.engine._create_flat_position_report(
        ctx.instrument.id,
        ctx.client.account_id,
    )

    assert report._arb_reconciliation_snapshot is snapshot
    assert ctx.engine._reconcile_position_report(report) is False
    assert parent_calls == []
