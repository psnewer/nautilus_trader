"""ArbLiveExecutionEngine —— NT LiveExecutionEngine 薄子类。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field

import pandas as pd

from nautilus_trader.common.enums import LogLevel
from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import GenerateOrderStatusReports
from nautilus_trader.execution.messages import GeneratePositionStatusReports
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.live.execution_engine import LiveExecutionEngine
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.events import OrderCancelRejected
from nautilus_trader.model.identifiers import InstrumentId
from src.arbitrage.common.opportunity import CANCEL_OPPORTUNITY_PARAM
from src.arbitrage.common.opportunity import RISK_LEG_DENIED_TOPIC
from src.arbitrage.common.opportunity import CancelOpportunityMeta
from src.arbitrage.common.opportunity import OpportunityMeta
from src.arbitrage.common.opportunity import cancel_meta_from_command
from src.arbitrage.common.opportunity import meta_from_order
from src.arbitrage.common.positions import pair_positions_digest
from src.arbitrage.execution.reconciliation import ReconciliationStateSnapshot
from src.arbitrage.execution.reconciliation import attach_reconciliation_snapshot


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
        self._arb_venue_liveness = None
        self._arb_command_groups: dict[tuple[str, str], _CommandGroupContext] = {}
        self._arb_position_reconciliation_snapshots = {}
        self._msgbus.subscribe(topic=RISK_LEG_DENIED_TOPIC, handler=self._on_opportunity_leg_denied)

    def configure_arb(
        self,
        *,
        pair_registry=None,
        venue_liveness=None,
        barrier_timeout_secs: float | None = None,
    ) -> None:
        if pair_registry is not None:
            self._pair_registry = pair_registry
        if venue_liveness is not None:
            self._arb_venue_liveness = venue_liveness
        if barrier_timeout_secs is not None:
            self._arb_barrier_timeout_ns = secs_to_nanos(barrier_timeout_secs)

    def _resolve_inflight_order(self, order) -> None:
        if order.status != OrderStatus.PENDING_CANCEL:
            super()._resolve_inflight_order(order)
            return

        # 与 SUBMITTED 的 UNKNOWN reject 语义对齐：查询不到状态不能证明撤单成功。
        # cancel reject 会让 NT FSM 恢复撤单前的 ACCEPTED/PARTIALLY_FILLED 状态，
        # 后续策略仍能把真实残单识别为 open 并再次走 cancel-only。
        ts_now = self._clock.timestamp_ns()
        cancel_rejected = OrderCancelRejected(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=order.venue_order_id,
            account_id=order.account_id,
            reason="UNKNOWN",
            event_id=UUID4(),
            ts_event=ts_now,
            ts_init=ts_now,
            reconciliation=True,
        )
        self._log.debug(f"Generated {cancel_rejected}")
        self._handle_event_with_tracking(cancel_rejected)
        self._clear_recon_tracking(order.client_order_id)
        self._order_local_activity_ns.pop(order.client_order_id, None)

    async def _query_order_status_reports(self):
        order_status_start = self._clock.utc_now() - pd.Timedelta(
            minutes=self.open_check_lookback_mins,
        )
        clients = list(self._clients.values())
        batches = await asyncio.gather(
            *[
                client.generate_order_status_reports(
                    GenerateOrderStatusReports(
                        instrument_id=None,
                        start=order_status_start,
                        end=None,
                        open_only=self.open_check_open_only,
                        command_id=UUID4(),
                        ts_init=self._clock.timestamp_ns(),
                        log_receipt_level=LogLevel.DEBUG,
                    ),
                )
                for client in clients
            ],
            return_exceptions=True,
        )
        reports = []
        protected_order_ids = set()
        for client, batch in zip(clients, batches, strict=True):
            if isinstance(batch, Exception):
                self._mark_reconciliation_liveness(client, "order", alive=False)
                self._log.error(f"Failed to generate order status reports: {batch}")
                continue
            self._mark_reconciliation_liveness(client, "order", alive=True)
            # #318:逐 pair(报告与本地 open/inflight 单并入同一 scope)判 order_digest。通过的 pair 纳入
            # reports;stale 的 pair 只把该 pair 的本地 open/inflight 单塞进 venue_reported_ids 保护
            # (挡 NT missing_at_venue 误 reject venue 上仍存活的合法单;空批也能凭本地单判 stale)。
            snapshot = getattr(batch, "snapshot", None)
            local_orders = [
                order
                for order in self._cache.orders(account_id=client.account_id)
                if order.is_open or order.is_inflight
            ]
            for instrument_ids, scoped_reports, scoped_local, _deferred in self._collect_scopes(
                batch,
                local_orders,
            ):
                if snapshot is not None and not snapshot.is_current_for_instruments(client, instrument_ids):
                    self._log.warning(
                        f"Discarding stale order reports before reconciliation: client={client.id}",
                    )
                    protected_order_ids.update(order.client_order_id for order in scoped_local)
                    continue
                reports.extend(scoped_reports)
        venue_reported_ids = {
            report.client_order_id
            for report in reports
            if report.client_order_id is not None
        }
        venue_reported_ids.update(protected_order_ids)
        return reports, venue_reported_ids

    async def _query_position_status_reports(self):
        clients = list(self._clients.values())
        self._arb_position_reconciliation_snapshots = {}
        batches = await asyncio.gather(
            *[
                client.generate_position_status_reports(
                    GeneratePositionStatusReports(
                        instrument_id=None,
                        start=None,
                        end=None,
                        command_id=UUID4(),
                        ts_init=self._clock.timestamp_ns(),
                        log_receipt_level=LogLevel.DEBUG,
                    ),
                )
                for client in clients
            ],
            return_exceptions=True,
        )
        venue_positions = {}
        failed_venues = set()
        for client, batch in zip(clients, batches, strict=True):
            if isinstance(batch, Exception):
                self._mark_reconciliation_liveness(client, "position", alive=False)
                failed_venues.add(client.venue)
                self._log.error(
                    f"Failed to generate position status reports for venue {client.venue}: {batch}",
                )
                continue
            self._mark_reconciliation_liveness(client, "position", alive=True)
            # #318:逐 pair(报告与本地 position 并入同一 scope)判 position_digest(含 realized_pnl)。
            # 通过的 pair 纳入 venue_positions 并选择性更新其 offset;有 stale pair → venue 记 failed
            # (保守:跳过该 venue 的 cached-position flatten,免误平未验证的 stale pair;空批凭本地仓判)。
            snapshot = getattr(batch, "snapshot", None)
            local_positions = list(
                self._cache.positions(venue=client.venue, account_id=client.account_id) or (),
            )
            payload = getattr(batch, "payload", None)
            deferred_instruments = payload.keys() if isinstance(payload, dict) else ()
            passed_reports = []
            passed_instruments = set()
            any_stale = False
            for instrument_ids, scoped_reports, _scoped_local, scoped_deferred in self._collect_scopes(
                batch,
                local_positions,
                deferred_instruments,
            ):
                if snapshot is not None and not snapshot.is_current_for_instruments(client, instrument_ids):
                    any_stale = True
                    self._log.warning(
                        f"Discarding stale position reports before reconciliation: client={client.id}",
                    )
                    continue
                passed_reports.extend(scoped_reports)
                passed_instruments.update(str(report.instrument_id) for report in scoped_reports)
                passed_instruments.update(scoped_deferred)
            if any_stale:
                failed_venues.add(client.venue)
            # realized offset:只对通过校验的 instrument 选择性更新(offset 公式不变),与 position 同批做。
            self._apply_reconciliation_batch(client, "position", batch, passed_instruments)
            if snapshot is not None and passed_reports:
                fresh = ReconciliationStateSnapshot.capture(client, kind="position")
                self._arb_position_reconciliation_snapshots[client.venue] = fresh
                for report in passed_reports:
                    attach_reconciliation_snapshot(report, fresh)
            for report in passed_reports:
                venue_positions[report.instrument_id] = report
        return venue_positions, failed_venues

    def _reconcile_execution_mass_status(self, mass_status):
        # #318:启动 mass-status 同样 per-pair —— report 已由 `_guard_reconciliation_reports` 附上 snapshot,
        # super 逐 report 走 `_reconciliation_report_is_current`(按 pair 判),stale pair 的 report 被拦、
        # 通过的照常应用。offset 用预检出的通过 instrument 选择性更新。
        client = self._clients.get(mass_status.client_id)
        batches = getattr(mass_status, "_arb_reconciliation_batches", {})
        for kind, batch in batches.items():
            payload = getattr(batch, "payload", None)
            deferred_instruments = (
                payload.keys()
                if kind == "position" and isinstance(payload, dict)
                else ()
            )
            _passed, passed_instruments = self._partition_batch_by_pair(
                client,
                batch,
                deferred_instruments,
            )
            self._apply_reconciliation_batch(client, kind, batch, passed_instruments)
        return super()._reconcile_execution_mass_status(mass_status)

    def _mark_reconciliation_liveness(self, client, kind: str, *, alive: bool) -> None:
        """按远端查询结果更新对应维度;本地报告应用不参与判定。"""
        liveness = self._arb_venue_liveness
        if liveness is None or client is None:
            return
        method = getattr(liveness, f"mark_{kind}_{'alive' if alive else 'dead'}")
        method(client.venue)

    def _reconcile_order_report(self, report, trades, is_external: bool = True):
        if not self._reconciliation_report_is_current(report):
            return False
        return super()._reconcile_order_report(report, trades, is_external)

    def _reconcile_position_report(self, report):
        if not self._reconciliation_report_is_current(report):
            return False
        return super()._reconcile_position_report(report)

    def _create_flat_position_report(self, instrument_id, account_id):
        report = super()._create_flat_position_report(instrument_id, account_id)
        snapshot = self._arb_position_reconciliation_snapshots.get(instrument_id.venue)
        if snapshot is not None:
            attach_reconciliation_snapshot(report, snapshot)
        return report

    def _reconciliation_report_is_current(self, report) -> bool:
        snapshot = getattr(report, "_arb_reconciliation_snapshot", None)
        if snapshot is None:
            return True
        client = next(
            (item for item in self._clients.values() if item.account_id == report.account_id),
            None,
        )
        # #318:按该 report 所属 pair 判(pair 未知则退化为单 instrument),不再账户全量。
        if client is not None and snapshot.is_current_for_instruments(
            client,
            self._instruments_to_check_for(report.instrument_id),
        ):
            return True
        self._log.warning(
            "Discarding stale execution report before reconciliation: "
            f"report_id={getattr(report, 'id', '?')}",
        )
        return False

    @staticmethod
    def _apply_reconciliation_batch(client, kind: str, batch, applied_instruments=None) -> None:
        apply_batch = getattr(client, "apply_reconciliation_batch", None)
        if apply_batch is not None:
            apply_batch(kind, batch, applied_instruments)

    # ── #318 per-pair reconcile 辅助 ──────────────────────────────────────
    def _scope_of(self, instrument_id):
        """该 instrument 的 staleness 判定 scope:(scope_key, instrument_ids)。

        pair 已知 → 该 pair 全部腿(整 pair 一致才算 current);pair 未知 → 仅自身。
        """
        registry = self._pair_registry
        if registry is not None:
            pair_id = registry.get(instrument_id)
            if pair_id is not None:
                ids = registry.instrument_ids_for_pair(pair_id)
                if ids:
                    return ("pair", pair_id), set(ids)
        return ("iid", str(instrument_id)), {str(instrument_id)}

    def _instruments_to_check_for(self, instrument_id) -> set:
        return self._scope_of(instrument_id)[1]

    def _collect_scopes(self, reports, locals_, deferred_instrument_ids=()):
        """把 reports 与本地对象(order/position)按 scope 归组。

        deferred instrument 是没有 PositionReport 的 realized snapshot 候选,同样归入 scope
        做 staleness 校验。返回 [(instrument_ids, [reports], [locals], {deferred ids})]。
        """
        scopes: dict = {}
        for report in reports:
            key, ids = self._scope_of(report.instrument_id)
            scopes.setdefault(key, [ids, [], [], set()])[1].append(report)
        for obj in locals_ or ():
            key, ids = self._scope_of(obj.instrument_id)
            scopes.setdefault(key, [ids, [], [], set()])[2].append(obj)
        for instrument_id in deferred_instrument_ids:
            key, ids = self._scope_of(instrument_id)
            scopes.setdefault(key, [ids, [], [], set()])[3].add(str(instrument_id))
        return [
            (ids, grp_reports, grp_local, grp_deferred)
            for ids, grp_reports, grp_local, grp_deferred in scopes.values()
        ]

    def _partition_batch_by_pair(self, client, batch, deferred_instrument_ids=()):
        """仅 report 驱动的 per-pair 通过判定(mass-status 用):返回 (通过的 reports, 通过的 instrument 集合)。"""
        snapshot = getattr(batch, "snapshot", None)
        passed_reports = []
        passed_instruments = set()
        for instrument_ids, scoped_reports, _, scoped_deferred in self._collect_scopes(
            batch,
            (),
            deferred_instrument_ids,
        ):
            if snapshot is not None and not snapshot.is_current_for_instruments(client, instrument_ids):
                continue
            passed_reports.extend(scoped_reports)
            passed_instruments.update(str(report.instrument_id) for report in scoped_reports)
            passed_instruments.update(scoped_deferred)
        return passed_reports, passed_instruments

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
            blocked = self._other_execution_in_flight_for_pair(meta.pair_id)
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
            blocked = self._other_execution_in_flight_for_pair(meta.pair_id)
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
        if meta.positions_digest != ctx.meta.positions_digest:
            self._deny_order(command.order, "opportunity metadata mismatch: positions_digest differs")
            self._finish(ctx, terminal="denied")
            return
        if self._add_group_command(ctx, meta.leg_key, command):
            self._release(ctx)

    def _other_execution_in_flight_for_pair(self, pair_id: str) -> bool:
        """#316 per-pair ≤1 执行的**唯一**判定,只读派生态 —— 无 token、无出口、不可能泄漏。

        两个源(都按 `pair_id` 过滤):① grouped barrier 里还在等命令的**同 pair** submit/cancel group
        (跳过 `terminal is not None` 的墓碑,否则墓碑会挡住合法新机会);② 任一 exec client 上**归属该 pair**
        的在飞 session。跨 pair 并发执行放行,同 pair 仍串行(每 pair ≤1 机会)。

        **取 `pair_id`**:本方法只在 `ctx is None`(本 pair 全新机会)时调用,同 pair 的在飞执行必是同 pair
        的另一次机会;归属只到 pair 级即够 —— ctx 带 `pair_id`,session 存 `pair_id`,tag-less 残单用
        `PairRegistry.get(instrument_id)` 反查兜底(instrument 已注销则不归属任何 pair,fail-open,§7.5)。

        **无需加锁**:所有 `SubmitOrder` / `CancelOrder` 经 `LiveExecutionEngine` 的单队列单 task 逐条
        `_execute_command`,构造上串行,本判定天然原子。
        """
        for ctx in self._arb_command_groups.values():
            if ctx.terminal is None and ctx.pair_id == pair_id:
                return True
        resolve = self._pair_registry.get if self._pair_registry is not None else (lambda _iid: None)
        for client in self._clients.values():
            fn = getattr(client, "_pair_execution_active", None)
            if fn is not None and fn(pair_id, resolve):
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
        # #317:只比 position digest(order-digest 已删,承 #316 per-pair ≤1)。威胁安全的 order 变化=成交,
        # 成交必改 position → 这里抓住;纯撤单不动 position、只让新机会保守一轮(自愈),不再误拒。
        positions_baseline = meta.positions_digest
        instrument_ids = self._residual_check_instrument_ids(ctx)
        current_positions = pair_positions_digest(self._cache, instrument_ids)
        if positions_baseline is None or current_positions != positions_baseline:
            reason = (
                "opportunity denied: pair positions changed during evaluation"
                if positions_baseline is not None
                else "opportunity denied: missing pair execution-state baseline"
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
