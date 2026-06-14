"""ArbLiveExecutionEngine —— NT LiveExecutionEngine 薄子类。"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.live.execution_engine import LiveExecutionEngine

from src.arbitrage.common.opportunity import OpportunityMeta
from src.arbitrage.common.opportunity import RISK_LEG_DENIED_TOPIC
from src.arbitrage.common.opportunity import meta_from_order


_TIMEOUT_PREFIX = "arb_opp_timeout:"


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
        self._arb_opportunities: dict[str, _OpportunityContext] = {}
        self._msgbus.subscribe(topic=RISK_LEG_DENIED_TOPIC, handler=self._on_opportunity_leg_denied)

    def configure_arb(self, *, pair_inflight=None, barrier_timeout_secs: float | None = None) -> None:
        self._arb_pair_inflight = pair_inflight
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
        ctx.terminal = "released"
        self._cancel_timer(ctx)
        self._arb_opportunities.pop(ctx.meta.opportunity_id, None)
        for leg_key in ctx.meta.expected_legs:
            command = ctx.allowed.get(leg_key)
            if command is not None:
                super()._execute_command(command)

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
