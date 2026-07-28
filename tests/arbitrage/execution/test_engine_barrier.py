"""ArbLiveExecutionEngine opportunity barrier 单测。"""

from __future__ import annotations

import asyncio

from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Quantity
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.test_kit.mocks.exec_clients import MockExecutionClient
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.trading.strategy import Strategy
from src.arbitrage.common.open_orders import pair_open_orders_digest
from src.arbitrage.common.opportunity import RISK_LEG_DENIED_TOPIC
from src.arbitrage.common.opportunity import OpportunityMeta
from src.arbitrage.common.positions import pair_positions_digest
from src.arbitrage.execution.engine import ArbLiveExecutionEngine
from src.arbitrage.execution.engine import _OpportunityContext
from tests.arbitrage.risk._factories import pm_instrument


class _RecordingExecutionClient(MockExecutionClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.residual_cancels = []

    def _cancel_residual_orders(self, instrument_id, residual: list) -> None:
        self.residual_cancels.append((instrument_id, list(residual)))


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
        open_orders_digest: str | None = None,
        positions_digest: str | None = None,
    ) -> SubmitOrder:
        baseline = open_orders_digest or pair_open_orders_digest(
            self.cache,
            [self.instrument.id],
        )
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
                f"arb:open_orders_digest={baseline}",
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


def test_barrier_waits_until_all_legs_pass_before_release():
    ctx = _Ctx()
    first = ctx.submit_cmd("pm:home:0")
    second = ctx.submit_cmd("oe:away:1")

    ctx.engine._execute_command(first)
    assert len(ctx.client.commands) == 0

    ctx.engine._execute_command(second)
    assert len(ctx.client.commands) == 2


def test_barrier_denies_all_legs_when_pair_open_orders_changed(monkeypatch):
    ctx = _Ctx()
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda event: denied.append(event))
    first = ctx.submit_cmd("pm:home:0", open_orders_digest="baseline")
    second = ctx.submit_cmd("oe:away:1", open_orders_digest="baseline")
    monkeypatch.setattr(
        "src.arbitrage.execution.engine.pair_open_orders_digest",
        lambda cache, instrument_ids: "changed",
    )

    ctx.engine._execute_command(first)
    ctx.engine._execute_command(second)

    assert len(ctx.client.commands) == 0
    assert len(_denied_reasons(denied)) == 2
    assert "opp-1" not in ctx.engine._arb_opportunities


def test_barrier_denies_legacy_opportunity_without_open_orders_baseline():
    ctx = _Ctx()
    first = ctx.submit_cmd("pm:home:0")
    second = ctx.submit_cmd("oe:away:1")
    first.order.tags[:] = [tag for tag in first.order.tags if "open_orders_digest" not in tag]
    second.order.tags[:] = [tag for tag in second.order.tags if "open_orders_digest" not in tag]

    ctx.engine._execute_command(first)
    ctx.engine._execute_command(second)

    assert len(ctx.client.commands) == 0


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
    assert "opp-1" not in ctx.engine._arb_opportunities


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
    ctx.engine._on_opportunity_timeout(type("Evt", (), {"name": "arb_opp_timeout:opp-1"})())

    assert len(ctx.client.commands) == 0
    assert denied


def test_barrier_cancel_only_blocks_all_new_submits_when_residual_and_no_cancel_leg():
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

    准入闸只在 ctx 创建(首腿)那一刻查 `_execution_active`,故 session 必须在首腿之后出现才落到本场景;
    末腿到达时 ctx 已存在、不再查闸,`_release`→`_cancel_only` 也全程不看 `_execution_active`。
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
    ctx.client._execution_active = True            # 中途:撤单/执行 session 冒出(多腿窗口)
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
    barrier_ctx = _OpportunityContext(
        meta=OpportunityMeta(
            opportunity_id="opp-1",
            pair_id="pair-1",
            leg_key="pm:away:0",
            expected_legs=("pm:away:0",),
        ),
        expected={"pm:away:0"},
        allowed={"pm:away:0": allowed},
    )

    residuals = ArbLiveExecutionEngine._opportunity_residuals(fake_engine, barrier_ctx)

    assert residuals == [(ctx.client, residual_instrument.id, [residual])]


def test_barrier_residual_with_explicit_cancel_leg_releases_normally():
    ctx = _Ctx()
    first = ctx.submit_cmd("pm:home:0", intent="cancel")
    second = ctx.submit_cmd("oe:away:1")
    residual = ctx.submit_cmd("pm:home:0").order
    ctx.engine._opportunity_residuals = (
        lambda barrier_ctx: [(ctx.client, ctx.instrument.id, [residual])]
    )

    ctx.engine._execute_command(first)
    ctx.engine._execute_command(second)

    assert len(ctx.client.commands) == 2
    assert ctx.client.residual_cancels == []


# ── #261:全局 ≤1 执行(barrier 单点 + 纯派生态)──────────────────────
def _denied_reasons(events):
    return [getattr(e, "reason", "") for e in events if type(e).__name__ == "OrderDenied"]


def test_second_opportunity_denied_while_first_waits_in_barrier():
    """派生源 ①:barrier 里还有非墓碑 ctx → 新机会整个丢弃。"""
    ctx = _Ctx()
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda e: denied.append(e))

    ctx.engine._execute_command(ctx.submit_cmd("pm:home:0"))          # opp-1 第一腿,等第二腿
    ctx.engine._execute_command(
        ctx.submit_cmd("pm:home:0", opportunity_id="opp-2", pair_id="pair-2"),
    )

    assert len(ctx.client.commands) == 0                              # 谁都没下到 venue
    assert any("another opportunity" in r or "denied" in r for r in _denied_reasons(denied))


def test_second_opportunity_denied_while_first_has_live_session():
    """派生源 ②:任一 client `_execution_active` → 新机会丢弃。

    这是 #254 依赖却当时不成立的那条保证:PM 关闭 accepted 预扣后,accepted→fill
    之间的无预扣窗口靠"同时只有一个机会在执行"兜底,而旧 per-pair 闸跨 pair 拦不住。
    """
    ctx = _Ctx()
    ctx.client._execution_active = True                               # 模拟已有 session 在飞
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda e: denied.append(e))

    ctx.engine._execute_command(
        ctx.submit_cmd("pm:home:0", opportunity_id="opp-2", pair_id="pair-2"),
    )

    assert len(ctx.client.commands) == 0
    assert _denied_reasons(denied)


def test_tombstone_denies_late_legs_and_pops_when_complete():
    """墓碑:被拒机会的后到腿立刻被拒;`denied` 集齐即提前 pop,不空等 barrier timer。

    没有墓碑的话会出现「B1 被拒 → A 执行结束 → B2 另建 ctx 空等 2s」,那个 ctx 期间
    会挡住合法新机会。
    """
    ctx = _Ctx()
    ctx.client._execution_active = True
    ctx.engine._execute_command(ctx.submit_cmd("pm:home:0", opportunity_id="opp-2"))
    assert ctx.engine._arb_opportunities["opp-2"].terminal == "denied"

    ctx.client._execution_active = False                              # 执行结束
    ctx.engine._execute_command(ctx.submit_cmd("oe:away:1", opportunity_id="opp-2"))

    # 后到腿没有另建 ctx、没有被放行;墓碑已随 denied 集齐被清掉
    assert len(ctx.client.commands) == 0
    assert "opp-2" not in ctx.engine._arb_opportunities


def test_tombstone_does_not_block_a_legitimate_new_opportunity():
    """墓碑自身必须被 `_other_execution_in_flight` 跳过,否则它会挡住别人。"""
    ctx = _Ctx()
    ctx.client._execution_active = True
    ctx.engine._execute_command(ctx.submit_cmd("pm:home:0", opportunity_id="opp-2"))
    assert ctx.engine._arb_opportunities["opp-2"].terminal == "denied"   # 墓碑还在

    ctx.client._execution_active = False
    assert ctx.engine._other_execution_in_flight() is False             # 墓碑不算在飞


def test_tombstone_is_reclaimed_by_barrier_timer_when_legs_never_complete():
    """结构保证:某腿根本没发出(如 submitter 的 instrument 缺失)→ `denied` 永远凑不齐,
    只能靠 barrier timer 回收。提前 pop 是路径,timer 是保证,不能只留路径。"""
    ctx = _Ctx()
    ctx.client._execution_active = True
    ctx.engine._execute_command(ctx.submit_cmd("pm:home:0", opportunity_id="opp-2"))
    assert "opp-2" in ctx.engine._arb_opportunities                     # 只到了一条腿

    ctx.engine._on_opportunity_timeout(
        type("Evt", (), {"name": "arb_opp_timeout:opp-2"})(),
    )
    assert "opp-2" not in ctx.engine._arb_opportunities


def test_new_opportunity_allowed_once_nothing_in_flight():
    """闸不是单向的:执行结束后新机会必须能正常通过(验非空断言)。"""
    ctx = _Ctx()
    ctx.client._execution_active = True
    ctx.engine._execute_command(ctx.submit_cmd("pm:home:0", opportunity_id="opp-2"))
    assert len(ctx.client.commands) == 0

    ctx.client._execution_active = False
    ctx.engine._execute_command(ctx.submit_cmd("pm:home:0", opportunity_id="opp-3"))
    ctx.engine._execute_command(ctx.submit_cmd("oe:away:1", opportunity_id="opp-3"))
    assert len(ctx.client.commands) == 2                                # 两腿都下到 venue


# ── #263:leg_denied 早于 sibling ctx 的竞态(拒单通知先到)──────────────
def test_leg_denied_before_sibling_ctx_leaves_no_orphan():
    """Risk 队列发的 leg_denied 早于 Exec 队列建 ctx → 建持久墓碑,sibling 腿命中被拒,不成孤儿。

    #263 前:`ctx is None → return` 丢弃拒单;sibling 腿随后建 ctx 成孤儿,占住全局执行槽
    直到 barrier 超时(#261 后阻断所有机会 → deny 风暴)。这里验证 sibling 腿到达时被立即拒、
    且机会即时清出 `_arb_opportunities`(denied 集齐),不占槽。
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
    assert "opp-1" in ctx.engine._arb_opportunities
    assert ctx.engine._other_execution_in_flight() is False

    # sibling(过 Risk 的那条腿)随后到 barrier
    sibling = ctx.submit_cmd("pm:home:0")
    ctx.engine._execute_command(sibling)

    assert len(ctx.client.commands) == 0                 # sibling 没下到 venue
    assert denied                                        # sibling 被拒
    assert "opp-1" not in ctx.engine._arb_opportunities  # denied 集齐 → 墓碑即时清,不占槽


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
    assert "opp-1" in ctx.engine._arb_opportunities      # 墓碑在等 sibling

    ctx.engine._on_opportunity_timeout(
        type("Evt", (), {"name": "arb_opp_timeout:opp-1"})(),
    )
    assert "opp-1" not in ctx.engine._arb_opportunities  # timer 回收


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
    assert "opp-1" not in ctx.engine._arb_opportunities  # 没 expected → 立即清
