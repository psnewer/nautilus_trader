"""ArbLiveExecutionEngine —— NT LiveExecutionEngine 薄子类。"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.live.execution_engine import LiveExecutionEngine
from nautilus_trader.model.identifiers import InstrumentId

from src.arbitrage.common.opportunity import OpportunityMeta
from src.arbitrage.common.opportunity import RISK_LEG_DENIED_TOPIC
from src.arbitrage.common.opportunity import meta_from_order


_TIMEOUT_PREFIX = "arb_opp_timeout:"
_CANCEL_LEG_INTENTS = {"cancel", "cancel-only", "cancel_only"}


@dataclass(slots=True)
class _OpportunityContext:
    meta: OpportunityMeta
    expected: set[str]
    allowed: dict[str, SubmitOrder] = field(default_factory=dict)
    terminal: str | None = None


class ArbLiveExecutionEngine(LiveExecutionEngine):
    """同一 opportunity 的所有腿 risk-pass 后才 release 到 venue ExecutionClient。"""

    def __init__(self, *args, barrier_timeout_secs: float = 2.0, pair_inflight=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._arb_barrier_timeout_ns = secs_to_nanos(barrier_timeout_secs)
        self._arb_pair_inflight = pair_inflight
        self._pair_registry = None
        self._arb_opportunities: dict[str, _OpportunityContext] = {}
        self._msgbus.subscribe(topic=RISK_LEG_DENIED_TOPIC, handler=self._on_opportunity_leg_denied)

    def configure_arb(
        self,
        *,
        pair_inflight=None,
        pair_registry=None,
        barrier_timeout_secs: float | None = None,
    ) -> None:
        self._arb_pair_inflight = pair_inflight
        if pair_registry is not None:
            self._pair_registry = pair_registry
        if barrier_timeout_secs is not None:
            self._arb_barrier_timeout_ns = secs_to_nanos(barrier_timeout_secs)

    def _execute_command(self, command) -> None:
        if isinstance(command, SubmitOrder):
            meta = meta_from_order(command.order)
            if meta is not None:
                self._handle_opportunity_pass(meta, command)
                return
        super()._execute_command(command)

    def _handle_opportunity_pass(self, meta: OpportunityMeta, command: SubmitOrder) -> None:
        ctx = self._arb_opportunities.get(meta.opportunity_id)
        if ctx is None:
            ctx = _OpportunityContext(meta=meta, expected=set(meta.expected_legs))
            self._arb_opportunities[meta.opportunity_id] = ctx
            self._clock.set_time_alert_ns(
                name=f"{_TIMEOUT_PREFIX}{meta.opportunity_id}",
                alert_time_ns=self._clock.timestamp_ns() + self._arb_barrier_timeout_ns,
                callback=self._on_opportunity_timeout,
            )
        if ctx.terminal is not None:
            self._deny_order(command.order, f"opportunity already {ctx.terminal}")
            return
        if set(meta.expected_legs) != ctx.expected:
            self._deny_order(command.order, "opportunity metadata mismatch: expected_legs differ")
            self._finish(ctx, terminal="denied")
            return
        ctx.allowed[meta.leg_key] = command
        if set(ctx.allowed) >= ctx.expected:
            self._release(ctx)

    def _on_opportunity_leg_denied(self, msg) -> None:
        opportunity_id = (msg or {}).get("opportunity_id")
        if not opportunity_id:
            return
        ctx = self._arb_opportunities.get(opportunity_id)
        if ctx is None:
            pair_id = (msg or {}).get("pair_id")
            if pair_id and self._arb_pair_inflight is not None:
                ctx = _OpportunityContext(
                    meta=OpportunityMeta(
                        opportunity_id=str(opportunity_id),
                        pair_id=str(pair_id),
                        leg_key=str((msg or {}).get("leg_key") or "denied"),
                        expected_legs=(),
                    ),
                    expected=set(),
                )
                self._finish(ctx, terminal="denied")
            return
        self._finish(ctx, terminal="denied", reason=str((msg or {}).get("reason") or "risk denied"))

    def _on_opportunity_timeout(self, event) -> None:
        opportunity_id = event.name[len(_TIMEOUT_PREFIX):]
        ctx = self._arb_opportunities.get(opportunity_id)
        if ctx is not None:
            self._finish(ctx, terminal="timeout", reason="opportunity barrier timeout")

    def _release(self, ctx: _OpportunityContext) -> None:
        residuals = self._opportunity_residuals(ctx)
        if residuals and not self._has_cancel_leg(ctx):
            self._cancel_only(ctx, residuals)
            return

        ctx.terminal = "released"
        self._cancel_timer(ctx)
        self._arb_opportunities.pop(ctx.meta.opportunity_id, None)
        for leg_key in ctx.meta.expected_legs:
            command = ctx.allowed.get(leg_key)
            if command is not None:
                super()._execute_command(command)

    def _cancel_only(
        self,
        ctx: _OpportunityContext,
        residuals: list[tuple[object | None, object, list]],
    ) -> None:
        ctx.terminal = "cancel-only"
        self._cancel_timer(ctx)
        self._arb_opportunities.pop(ctx.meta.opportunity_id, None)

        reason = "opportunity cancel-only: residual open orders present"
        self._log.info(
            "Opportunity cancel-only: residual open orders present "
            f"opportunity_id={ctx.meta.opportunity_id}, pair_id={ctx.meta.pair_id}, "
            f"new_client_order_ids={[str(c.order.client_order_id) for c in ctx.allowed.values()]}, "
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

        for command in list(ctx.allowed.values()):
            self._deny_order(command.order, reason)
        # 上面 `_cancel_residual_orders` 已**同步**为每条残单起 cancel session(`_begin_cancel_session`
        # → `exec_started`;只有 venue IO 是 create_task),故这里通常 `exec_count>0` → **本行 no-op**,
        # 闸由撤单终态/watchdog 的 `exec_finished` 释放(撤单与下单同走 session,不特殊处理)。
        # 保留本行是兜**一个 cancel session 都没起**的情况:client 无 `_cancel_residual_orders`(:142
        # `continue`),或每条残单的 coid 已有 active session(`_begin_cancel_session` 返 False)。
        # `release_eval` 的「exec_count>0 则 no-op」契约使其可无条件调用而不误清(synchronization §7.3)。
        if self._arb_pair_inflight is not None and ctx.meta.pair_id:
            self._arb_pair_inflight.release_eval(ctx.meta.pair_id)

    def _opportunity_residuals(self, ctx: _OpportunityContext) -> list[tuple[object | None, object, list]]:
        residuals: list[tuple[object | None, object, list]] = []
        seen_instruments = set()
        allowed_by_instrument = {
            str(command.order.instrument_id): command
            for command in ctx.allowed.values()
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

    def _residual_check_instrument_ids(self, ctx: _OpportunityContext) -> list:
        registry = getattr(self, "_pair_registry", None)
        if registry is not None and ctx.meta.pair_id and hasattr(registry, "instrument_ids_for_pair"):
            instrument_ids = list(registry.instrument_ids_for_pair(ctx.meta.pair_id))
            if instrument_ids:
                return instrument_ids
        return [
            command.order.instrument_id
            for leg_key in ctx.meta.expected_legs
            for command in [ctx.allowed.get(leg_key)]
            if command is not None
        ]

    def _client_for_instrument(self, instrument_id):
        routing_map = getattr(self, "_routing_map", None)
        if routing_map is not None:
            client = routing_map.get(instrument_id.venue)
            if client is not None:
                return client
        return getattr(self, "_default_client", None)

    @staticmethod
    def _has_cancel_leg(ctx: _OpportunityContext) -> bool:
        for command in ctx.allowed.values():
            meta = meta_from_order(command.order)
            if meta is not None and meta.intent in _CANCEL_LEG_INTENTS:
                return True
        return False

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

    def _finish(self, ctx: _OpportunityContext, *, terminal: str, reason: str | None = None) -> None:
        ctx.terminal = terminal
        self._cancel_timer(ctx)
        self._arb_opportunities.pop(ctx.meta.opportunity_id, None)
        deny_reason = reason or f"opportunity {terminal}"
        for command in list(ctx.allowed.values()):
            self._deny_order(command.order, deny_reason)
        if self._arb_pair_inflight is not None and ctx.meta.pair_id:
            self._arb_pair_inflight.release_eval(ctx.meta.pair_id)

    def _cancel_timer(self, ctx: _OpportunityContext) -> None:
        try:
            self._clock.cancel_timer(f"{_TIMEOUT_PREFIX}{ctx.meta.opportunity_id}")
        except Exception:
            pass


def _coerce_instrument_id(value):
    if isinstance(value, InstrumentId):
        return value
    return InstrumentId.from_str(str(value))
