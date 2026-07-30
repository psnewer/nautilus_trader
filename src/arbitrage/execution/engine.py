"""ArbLiveExecutionEngine —— NT LiveExecutionEngine 薄子类。"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.live.execution_engine import LiveExecutionEngine
from nautilus_trader.model.identifiers import InstrumentId
from src.arbitrage.common.open_orders import pair_open_orders_digest
from src.arbitrage.common.opportunity import CANCEL_OPPORTUNITY_PARAM
from src.arbitrage.common.opportunity import CancelOpportunityMeta
from src.arbitrage.common.opportunity import RISK_LEG_DENIED_TOPIC
from src.arbitrage.common.opportunity import OpportunityMeta
from src.arbitrage.common.opportunity import cancel_meta_from_command
from src.arbitrage.common.opportunity import meta_from_order
from src.arbitrage.common.positions import pair_positions_digest


_GROUP_TIMEOUT_PREFIX = "arb_group_timeout:"
_SUBMIT_GROUP = "submit"
_CANCEL_GROUP = "cancel"


@dataclass(slots=True)
class _CommandGroupContext:
    kind: str
    group_id: str
    pair_id: str
    meta: OpportunityMeta | CancelOpportunityMeta
    expected: set[str]
    commands: dict[str, SubmitOrder | CancelOrder] = field(default_factory=dict)
    # 已生成本地失败终态的 key。不可复用 commands，否则 finish 会对同一命令重复发终态。
    terminal_keys: set[str] = field(default_factory=set)
    terminal: str | None = None


class ArbLiveExecutionEngine(LiveExecutionEngine):
    """统一收齐同组 SubmitOrder/CancelOrder，再按命令类型执行对应 release 策略。"""

    def __init__(self, *args, barrier_timeout_secs: float = 2.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._arb_barrier_timeout_ns = secs_to_nanos(barrier_timeout_secs)
        self._pair_registry = None
        self._arb_command_groups: dict[tuple[str, str], _CommandGroupContext] = {}
        self._msgbus.subscribe(topic=RISK_LEG_DENIED_TOPIC, handler=self._on_opportunity_leg_denied)

    def configure_arb(
        self,
        *,
        pair_registry=None,
        barrier_timeout_secs: float | None = None,
    ) -> None:
        if pair_registry is not None:
            self._pair_registry = pair_registry
        if barrier_timeout_secs is not None:
            self._arb_barrier_timeout_ns = secs_to_nanos(barrier_timeout_secs)

    def _execute_command(self, command) -> None:
        if isinstance(command, CancelOrder):
            meta = cancel_meta_from_command(command)
            if meta is not None:
                self._handle_cancel_opportunity(meta, command)
                return
            if CANCEL_OPPORTUNITY_PARAM in (command.params or {}):
                self._reject_cancel(command, "invalid cancel opportunity metadata")
                return
        if isinstance(command, SubmitOrder):
            meta = meta_from_order(command.order)
            if meta is not None:
                self._handle_opportunity_pass(meta, command)
                return
        super()._execute_command(command)

    def _handle_cancel_opportunity(self, meta: CancelOpportunityMeta, command: CancelOrder) -> None:
        ctx = self._get_group(_CANCEL_GROUP, meta.opportunity_id)
        if ctx is None:
            blocked = self._other_execution_in_flight()
            ctx = self._create_group(
                kind=_CANCEL_GROUP,
                group_id=meta.opportunity_id,
                pair_id=meta.pair_id,
                meta=meta,
                expected=meta.expected_cancels,
            )
            if blocked:
                ctx.terminal = "denied"
                self._log.info(
                    "Cancel opportunity denied: another opportunity is executing "
                    f"opportunity_id={meta.opportunity_id}, pair_id={meta.pair_id}",
                )
        if ctx.terminal is not None:
            self._reject_cancel(command, f"cancel opportunity already {ctx.terminal}")
            self._record_terminal_key(ctx, meta.cancel_key)
            return
        if meta.pair_id != ctx.pair_id or set(meta.expected_cancels) != ctx.expected:
            self._reject_cancel(command, "cancel opportunity metadata mismatch")
            self._finish_cancel(ctx, terminal="denied", reason="cancel opportunity metadata mismatch")
            return
        if self._add_group_command(ctx, meta.cancel_key, command):
            self._release_cancel(ctx)

    def _release_cancel(self, ctx: _CommandGroupContext) -> None:
        ctx.terminal = "released"
        self._close_group(ctx)
        meta = ctx.meta
        assert isinstance(meta, CancelOpportunityMeta)
        try:
            for cancel_key in meta.expected_cancels:
                command = ctx.commands.get(cancel_key)
                if command is None:
                    continue
                order = self._cache.order(command.client_order_id)
                if order is not None and order.is_closed:
                    self._log.info(
                        "Cancel opportunity target already closed; skip venue cancel "
                        f"opportunity_id={ctx.group_id}, "
                        f"client_order_id={command.client_order_id}, status={order.status_string()}",
                    )
                    continue
                super()._execute_command(command)
        finally:
            ctx.commands.clear()

    def _reject_cancel(self, command: CancelOrder, reason: str) -> None:
        client = self._find_client_for_command(command)
        if client is None:
            self._log.error(
                "Cannot reject cancel command: no execution client found "
                f"client_order_id={command.client_order_id}, reason={reason}",
            )
            return
        client.generate_order_cancel_rejected(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            venue_order_id=command.venue_order_id,
            reason=reason,
            ts_event=self._clock.timestamp_ns(),
        )

    def _finish_cancel(
        self,
        ctx: _CommandGroupContext,
        *,
        terminal: str,
        reason: str,
    ) -> None:
        ctx.terminal = terminal
        self._close_group(ctx)
        for command in list(ctx.commands.values()):
            self._reject_cancel(command, reason)

    def _handle_opportunity_pass(self, meta: OpportunityMeta, command: SubmitOrder) -> None:
        ctx = self._get_group(_SUBMIT_GROUP, meta.opportunity_id)
        if ctx is None:
            blocked = self._other_execution_in_flight()
            ctx = self._create_group(
                kind=_SUBMIT_GROUP,
                group_id=meta.opportunity_id,
                pair_id=meta.pair_id,
                meta=meta,
                expected=meta.expected_legs,
            )
            # timer 必须照常 arm —— 包括下面标 denied 的墓碑。若为墓碑另开一条"不 arm"的捷径,
            # 它就永远清不掉(`denied` 凑不齐时无人回收),正是 #260 反复踩到的那类泄漏。
            if blocked:
                # #261 全局 ≤1 执行:已有别的机会在执行 → 整个机会丢弃(不重试不排队,
                # 下一个 OBD tick 重评,同 cancel-only 既有纪律)。标 terminal 让后到的腿
                # 命中下面的分支被立刻拒,避免"先拒一条腿 → 执行结束 → 后一条腿另建 ctx 空等"。
                ctx.terminal = "denied"
                self._log.info(
                    "Opportunity denied: another opportunity is executing "
                    f"opportunity_id={meta.opportunity_id}, pair_id={meta.pair_id}",
                )
        if ctx.terminal is not None:
            self._deny_order(command.order, f"opportunity already {ctx.terminal}")
            self._record_terminal_key(ctx, meta.leg_key)
            return
        if set(meta.expected_legs) != ctx.expected:
            self._deny_order(command.order, "opportunity metadata mismatch: expected_legs differ")
            self._finish(ctx, terminal="denied")
            return
        if meta.open_orders_digest != ctx.meta.open_orders_digest:
            self._deny_order(command.order, "opportunity metadata mismatch: open_orders_digest differs")
            self._finish(ctx, terminal="denied")
            return
        if meta.positions_digest != ctx.meta.positions_digest:
            self._deny_order(command.order, "opportunity metadata mismatch: positions_digest differs")
            self._finish(ctx, terminal="denied")
            return
        if self._add_group_command(ctx, meta.leg_key, command):
            self._release(ctx)

    def _other_execution_in_flight(self) -> bool:
        """#261 全局 ≤1 执行的**唯一**判定,只读派生态 —— 无 token、无出口、不可能泄漏。

        两个源:① grouped barrier 里还在等命令的 submit/cancel group(跳过
        `terminal is not None` 的墓碑,否则墓碑会挡住合法新机会);② 任一 exec client 的
        `_execution_active`(= 在飞 session 数 > 0)。

        **无需参数**:本方法只在 `ctx is None`(全新机会)时调用,故在飞的执行必然不是它的
        —— 各业务出口最终都调用 `_close_group`,所以 registry 中有非墓碑 ctx 等价于仍有
        group 在等命令。方案不依赖识别执行归属
        (`_active_sessions` 只存 `pair_id`,本就无法回答"属于哪次机会")。

        **无需加锁**:所有 `SubmitOrder` / `CancelOrder` 经 `LiveExecutionEngine` 的单队列单 task 逐条
        `_execute_command`,构造上串行,本判定天然原子。
        """
        for ctx in self._arb_command_groups.values():
            if ctx.terminal is None:
                return True
        for client in self._clients.values():
            if getattr(client, "_execution_active", False):
                return True
        return False

    def _create_group(
        self,
        *,
        kind: str,
        group_id: str,
        pair_id: str,
        meta: OpportunityMeta | CancelOpportunityMeta,
        expected,
    ) -> _CommandGroupContext:
        ctx = _CommandGroupContext(
            kind=kind,
            group_id=group_id,
            pair_id=pair_id,
            meta=meta,
            expected=set(expected),
        )
        self._arb_command_groups[(kind, group_id)] = ctx
        self._arm_group_timer(ctx)
        return ctx

    def _get_group(self, kind: str, group_id: str) -> _CommandGroupContext | None:
        return self._arb_command_groups.get((kind, group_id))

    @staticmethod
    def _add_group_command(
        ctx: _CommandGroupContext,
        command_key: str,
        command: SubmitOrder | CancelOrder,
    ) -> bool:
        ctx.commands[command_key] = command
        return set(ctx.commands) >= ctx.expected

    def _record_terminal_key(self, ctx: _CommandGroupContext, command_key: str) -> None:
        ctx.terminal_keys.add(command_key)
        if ctx.terminal_keys >= ctx.expected:
            self._close_group(ctx)

    def _arm_group_timer(self, ctx: _CommandGroupContext) -> None:
        self._clock.set_time_alert_ns(
            name=f"{_GROUP_TIMEOUT_PREFIX}{ctx.kind}:{ctx.group_id}",
            alert_time_ns=self._clock.timestamp_ns() + self._arb_barrier_timeout_ns,
            callback=self._on_group_timeout,
        )

    def _close_group(self, ctx: _CommandGroupContext) -> None:
        self._arb_command_groups.pop((ctx.kind, ctx.group_id), None)
        try:
            self._clock.cancel_timer(f"{_GROUP_TIMEOUT_PREFIX}{ctx.kind}:{ctx.group_id}")
        except Exception:
            pass

    def _on_group_timeout(self, event) -> None:
        suffix = event.name[len(_GROUP_TIMEOUT_PREFIX):]
        kind, separator, group_id = suffix.partition(":")
        if not separator:
            return
        ctx = self._get_group(kind, group_id)
        if ctx is None:
            return
        if kind == _SUBMIT_GROUP:
            self._finish(ctx, terminal="timeout", reason="opportunity barrier timeout")
        elif kind == _CANCEL_GROUP:
            self._finish_cancel(
                ctx,
                terminal="timeout",
                reason="cancel opportunity barrier timeout",
            )

    def _on_opportunity_leg_denied(self, msg) -> None:
        opportunity_id = (msg or {}).get("opportunity_id")
        if not opportunity_id:
            return
        ctx = self._get_group(_SUBMIT_GROUP, opportunity_id)
        if ctx is not None:
            # ctx 已存在(sibling 腿先到并建了 ctx)→ 任一腿被拒即整机会作废,立即收口。
            self._finish(ctx, terminal="denied", reason=str((msg or {}).get("reason") or "risk denied"))
            return
        # #263:leg_denied 早于 sibling ctx 的竞态 —— Risk 队列(发拒单)与 Exec 队列(建 ctx)
        # 是两条独立 async 队列,拒单通知可能先到。若直接 return,denied 记号丢失,sibling 腿随后
        # 建 ctx 会成孤儿,占住全局执行槽直到 barrier 超时(#261 后更会阻断所有机会 → deny 风暴)。
        # 故建**持久墓碑**:sibling 腿到 `_handle_opportunity_pass` 命中 `terminal="denied"` 立即被拒。
        # 复用 #261 墓碑机制;timer 照常 arm 作结构兜底(sibling 万一不来也能回收)。
        expected = tuple(str(v) for v in ((msg or {}).get("expected_legs") or ()))
        leg_key = str((msg or {}).get("leg_key") or "denied")
        meta = OpportunityMeta(
            opportunity_id=str(opportunity_id),
            pair_id=str((msg or {}).get("pair_id") or ""),
            leg_key=leg_key,
            expected_legs=expected,
        )
        ctx = self._create_group(
            kind=_SUBMIT_GROUP,
            group_id=str(opportunity_id),
            pair_id=meta.pair_id,
            meta=meta,
            expected=expected,
        )
        ctx.terminal = "denied"
        ctx.terminal_keys.add(leg_key)
        # expected 已知且此刻就集齐(单腿机会 / 全部腿都在 Risk 被拒)→ 立即清,不留墓碑等 timer。
        # expected 为空(非 arb / 旧格式消息)时 `denied >= set()` 恒真 → 同样立即清,退化为无竞态保护。
        if ctx.terminal_keys >= ctx.expected:
            self._close_group(ctx)

    def _release(self, ctx: _CommandGroupContext) -> None:
        residuals = self._opportunity_residuals(ctx)
        if residuals:
            self._cancel_only(ctx, residuals)
            return

        meta = ctx.meta
        assert isinstance(meta, OpportunityMeta)
        baseline = meta.open_orders_digest
        positions_baseline = meta.positions_digest
        instrument_ids = self._residual_check_instrument_ids(ctx)
        current = pair_open_orders_digest(self._cache, instrument_ids)
        current_positions = pair_positions_digest(self._cache, instrument_ids)
        if (
            baseline is None
            or current != baseline
            or positions_baseline is None
            or current_positions != positions_baseline
        ):
            reason = (
                "opportunity denied: pair open orders changed during evaluation"
                if baseline is not None and current != baseline
                else (
                    "opportunity denied: pair positions changed during evaluation"
                    if positions_baseline is not None and current_positions != positions_baseline
                    else "opportunity denied: missing pair execution-state baseline"
                )
            )
            self._log.info(
                f"{reason} opportunity_id={ctx.meta.opportunity_id}, "
                f"pair_id={ctx.pair_id}",
            )
            self._finish(ctx, terminal="denied", reason=reason)
            return

        ctx.terminal = "released"
        self._close_group(ctx)
        for leg_key in meta.expected_legs:
            command = ctx.commands.get(leg_key)
            if command is not None:
                super()._execute_command(command)

    def _cancel_only(
        self,
        ctx: _CommandGroupContext,
        residuals: list[tuple[object | None, object, list]],
    ) -> None:
        ctx.terminal = "cancel-only"
        self._close_group(ctx)

        reason = "opportunity cancel-only: residual open orders present"
        self._log.info(
            "Opportunity cancel-only: residual open orders present "
            f"opportunity_id={ctx.group_id}, pair_id={ctx.pair_id}, "
            f"new_client_order_ids={[str(c.order.client_order_id) for c in ctx.commands.values()]}, "
            f"residuals={self._format_residuals_for_log(residuals)}",
        )
        for client, instrument_id, orders in residuals:
            cancel_residual_orders = getattr(client, "_cancel_residual_orders", None)
            if cancel_residual_orders is None:
                self._log.warning(
                    "Opportunity cancel-only cannot cancel residual orders: "
                    f"client={client}, instrument_id={instrument_id}",
                )
                continue
            cancel_residual_orders(instrument_id, orders)

        for command in list(ctx.commands.values()):
            self._deny_order(command.order, reason)

    def _opportunity_residuals(self, ctx: _CommandGroupContext) -> list[tuple[object | None, object, list]]:
        residuals: list[tuple[object | None, object, list]] = []
        seen_instruments = set()
        allowed_by_instrument = {
            str(command.order.instrument_id): command
            for command in ctx.commands.values()
        }
        for raw_instrument_id in self._residual_check_instrument_ids(ctx):
            instrument_id = _coerce_instrument_id(raw_instrument_id)
            if instrument_id in seen_instruments:
                continue
            seen_instruments.add(instrument_id)
            open_orders = list(self._cache.orders_open(instrument_id=instrument_id) or [])
            if not open_orders:
                continue
            command = allowed_by_instrument.get(str(instrument_id))
            client = self._find_client_for_command(command) if command is not None else self._client_for_instrument(instrument_id)
            if client is None:
                self._log.error(
                    "Opportunity cancel-only found residual orders but no execution client: "
                    f"instrument_id={instrument_id}",
                )
            residuals.append((client, instrument_id, open_orders))
        return residuals

    def _residual_check_instrument_ids(self, ctx: _CommandGroupContext) -> list:
        registry = self._pair_registry      # `__init__` 无条件设为 None,不需要 getattr 兜
        if registry is not None and ctx.pair_id and hasattr(registry, "instrument_ids_for_pair"):
            instrument_ids = list(registry.instrument_ids_for_pair(ctx.pair_id))
            if instrument_ids:
                return instrument_ids
        meta = ctx.meta
        assert isinstance(meta, OpportunityMeta)
        return [
            command.order.instrument_id
            for leg_key in meta.expected_legs
            for command in [ctx.commands.get(leg_key)]
            if command is not None
        ]

    def _client_for_instrument(self, instrument_id):
        # `_routing_map` / `_default_client` 是 NT `ExecutionEngine` 的 cdef readonly 属性
        # (`engine.pxd:50,53`),`__init__` 无条件初始化 → 直接访问,缺失即应响亮报错。
        client = self._routing_map.get(instrument_id.venue)
        return client if client is not None else self._default_client

    @staticmethod
    def _format_residuals_for_log(residuals: list[tuple[object | None, object, list]]) -> list[dict]:
        out = []
        for _client, instrument_id, orders in residuals:
            out.append({
                "instrument_id": str(instrument_id),
                "orders": [
                    {
                        "client_order_id": str(getattr(order, "client_order_id", "")),
                        "venue_order_id": str(getattr(order, "venue_order_id", "")),
                    }
                    for order in orders
                ],
            })
        return out

    def _finish(
        self,
        ctx: _CommandGroupContext,
        *,
        terminal: str,
        reason: str | None = None,
    ) -> None:
        ctx.terminal = terminal
        self._close_group(ctx)
        deny_reason = reason or f"opportunity {terminal}"
        for command in list(ctx.commands.values()):
            self._deny_order(command.order, deny_reason)


def _coerce_instrument_id(value):
    if isinstance(value, InstrumentId):
        return value
    return InstrumentId.from_str(str(value))
