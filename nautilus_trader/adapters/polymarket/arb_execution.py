"""
ArbPolymarketExecutionClient —— PM 执行客户端薄子类(Q18c 宿主层)。

详细设计:`docs/arbitrage/architectures/execution/architecture.md §3.1 / §4.3 / §4.6`。

= 上游 `PolymarketExecutionClient`(订单 IO / 账户状态 / reports,直接复用)
  + `ArbExecutionSessionMixin`(session / 超时 / execution.* 同步)
  + `PolymarketSettlement`(#110:merge/redeem,由 **NT 连续 position 对账** 内 fire-and-forget 触发;
    **无 HealthCheckLoop** —— PM 健康检查已退役,对齐 OE #109)。

**位置(refactor.md #33 校准)**:本类是 PM venue-coupled 代码 → 住 PM adapter 目录(P9 唯一
例外:venue 适配器放 `nautilus_trader/adapters/<venue>/`)。**与上游 `execution.py` 同目录但不
同文件**(`arb_execution.py`)避免 upstream merge 冲突;import 上游类直接子类化。

**验证边界**:真 `ClobClient`/`ws_auth`/Data API 仍靠实盘;离线覆盖纯映射
`pm_raw_position_to_settlement` 以及 reports override 的 liveness / settlement fire-and-forget 接线。
"""

from __future__ import annotations

from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET
from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelOrder

from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.execution.session import ArbExecutionSessionMixin
from nautilus_trader.adapters.polymarket.settlement import PolymarketSettlement
from nautilus_trader.adapters.polymarket.settlement import SettlementPosition


class _RetryFailureRecorder:
    """记录 PM 上游 RetryManager 吞掉的查询失败。

    NT 上游 `RetryManager.run()` 失败时返回 None,部分 report 方法随后会把 None 当作空结果继续返回。
    Arb 层需要把这种"无真实 response"识别为 report 失败 → `mark_*_dead` + 返空(#122 对齐 OE;
    不再 raise,避免 startup reconciliation 被瞬时失败卡死)。

    ⚠️ 脆弱点:本类经"实例级替换 `self._retry_manager_pool` + monkeypatch 每个 acquired manager 的 `.run`、
    `finally` 还原"实现。**依赖 order-report 调用与其它走同 pool 的 report 调用不并发重叠**——重叠时
    全局替换/还原会互相覆盖。当前安全:NT 连续对账循环里同一 client 的各项 check 是同一 loop 迭代内串行
    `await`。改 NT 上游/引入并发前需复核;更稳的做法是 per-call 传 recorder 而非替换实例属性。
    """

    def __init__(self, pool, names: set[str]) -> None:
        self._pool = pool
        self._names = names
        self.failures: list[tuple[str, str | None]] = []

    async def acquire(self):
        manager = await self._pool.acquire()
        original_run = manager.run

        async def run(name, details, func, *args, **kwargs):
            result = await original_run(name, details, func, *args, **kwargs)
            if name in self._names and not manager.result:
                self.failures.append((name, manager.message))
            return result

        manager._arb_original_run = original_run
        manager.run = run
        return manager

    async def release(self, manager) -> None:
        original_run = getattr(manager, "_arb_original_run", None)
        if original_run is not None:
            manager.run = original_run
            del manager._arb_original_run
        await self._pool.release(manager)

    def __getattr__(self, name):
        return getattr(self._pool, name)


def pm_raw_position_to_settlement(item: dict) -> SettlementPosition:
    """#110:PM Data API `/positions` **原始 dict** → settlement 视图(纯映射,可单测)。
    键名按 Data API:`conditionId` / `size` / `negativeRisk` / `redeemable`(见 odds_client `_do_fetch_positions`)。"""
    return SettlementPosition(
        condition_id=str(item.get("conditionId", item.get("condition_id", "")) or ""),
        size=float(item.get("size", 0) or 0),
        neg_risk=bool(item.get("negativeRisk", False)),
        redeemable=bool(item.get("redeemable", False)),
    )


class ArbPolymarketExecutionClient(ArbExecutionSessionMixin, PolymarketExecutionClient):
    def __init__(
        self,
        loop,
        http_client,
        msgbus,
        cache,
        clock,
        instrument_provider,
        ws_auth,
        config,
        name=None,
        *,
        venue_liveness: VenueExecutionLiveness,
        pair_registry: PairRegistry | None = None,
        pair_inflight=None,  # PairInFlightGate(§6.10 §7);与 strategy 共享一份
        settlement: PolymarketSettlement | None = None,
        session_timeout_secs: float = 30.0,
    ) -> None:
        super().__init__(
            loop, http_client, msgbus, cache, clock,
            instrument_provider, ws_auth, config, name,
        )
        self._init_arb_session(
            session_timeout_secs=session_timeout_secs,
            pair_registry=pair_registry,
            pair_inflight=pair_inflight,
        )
        self._venue_liveness = venue_liveness
        self._settlement = settlement
        # #110:merge/redeem 改由 NT 连续 position 对账驱动(无 HealthCheckLoop)。
        # `_settlement_inflight` = single-flight 守卫(链上 tx 数秒,fire-and-forget 不阻塞对账循环,防并发重复提交)。
        self._settlement_inflight = False

    async def _submit_order(self, command) -> None:
        if not self._begin_session(command):
            return
        try:
            await super()._submit_order(command)
        except Exception as e:
            order = command.order
            self._log.error(
                f"Polymarket submit failed before venue acknowledgement "
                f"client_order_id={order.client_order_id}: {e!r}",
            )
            self.generate_order_denied(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=f"PM submit exception before venue acknowledgement: {e!r}",
                ts_event=self._clock.timestamp_ns(),
            )
            self._end_session(order.client_order_id)

    async def _cancel_residual_one(self, order) -> None:
        """#105:撤一条残单 —— 构 `CancelOrder` 走 `_cancel_order`。循环 + exec_count 跟踪由 base
        `_cancel_residual_orders`/`_tracked_residual_cancel` 统一(撤单纳入 exec_count,exec_count→0 才清 in-flight)。"""
        cmd = CancelOrder(
            trader_id=self.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=order.venue_order_id,
            command_id=UUID4(),
            ts_init=self._clock.timestamp_ns(),
        )
        await self._cancel_order(cmd)

    async def _run_settlement(self, raw_positions: list) -> None:
        """#110:后台跑 merge/redeem(fire-and-forget,不阻塞 NT 对账循环)。
        tx 失败仅 log(settlement.run 内吞),不判 venue dead;`finally` 清 single-flight 守卫。"""
        try:
            await self._settlement.run([pm_raw_position_to_settlement(p) for p in raw_positions])
        finally:
            self._settlement_inflight = False

    async def generate_order_status_reports(self, command):
        recorder = _RetryFailureRecorder(
            self._retry_manager_pool,
            {"generate_order_status_reports", "generate_fill_reports"},
        )
        original_pool = self._retry_manager_pool
        self._retry_manager_pool = recorder
        try:
            reports = await super().generate_order_status_reports(command)
        except Exception as e:
            # #122:对齐 OE —— 失败 mark_dead + 返空(不 raise)。raise 会让 startup reconciliation 失败
            # → trader 不启 → 全 actor 卡 READY;mark_dead 已达成 fail-closed(Risk 拦 PM 腿,靠后续成功对账自愈)。
            self._venue_liveness.mark_order_dead(POLYMARKET)
            if self._log is not None:
                self._log.warning(f"PM order reports query failed (mark dead, empty): {e!r}")
            return []
        finally:
            self._retry_manager_pool = original_pool
        if recorder.failures:
            self._venue_liveness.mark_order_dead(POLYMARKET)
            if self._log is not None:
                self._log.warning(f"PM order reports query failed (mark dead, empty): {recorder.failures!r}")
            return []
        self._venue_liveness.mark_order_alive(POLYMARKET)
        return reports

    async def generate_order_status_report(self, command):
        recorder = _RetryFailureRecorder(
            self._retry_manager_pool,
            {"generate_order_status_report"},
        )
        original_pool = self._retry_manager_pool
        self._retry_manager_pool = recorder
        try:
            report = await super().generate_order_status_report(command)
        except Exception as e:
            self._venue_liveness.mark_order_dead(POLYMARKET)  # #122:对齐 OE,失败返 None 不 raise
            if self._log is not None:
                self._log.warning(f"PM order report query failed (mark dead, None): {e!r}")
            return None
        finally:
            self._retry_manager_pool = original_pool
        if recorder.failures:
            self._venue_liveness.mark_order_dead(POLYMARKET)
            if self._log is not None:
                self._log.warning(f"PM order report query failed (mark dead, None): {recorder.failures!r}")
            return None
        self._venue_liveness.mark_order_alive(POLYMARKET)
        return report

    async def generate_position_status_reports(self, command):
        try:
            reports = await super().generate_position_status_reports(command)  # 单次拉 /positions,上游 stash raw
        except Exception as e:
            # #122:对齐 OE —— 失败 mark_dead + 返空(不 raise),否则 startup reconciliation 失败卡死启动。
            self._venue_liveness.mark_position_dead(POLYMARKET)
            if self._log is not None:
                self._log.warning(f"PM position reports query failed (mark dead, empty): {e!r}")
            return []
        self._venue_liveness.mark_position_alive(POLYMARKET)
        # #110:同一次拉的原始 /positions 跑 merge/redeem —— fire-and-forget + single-flight,不阻塞 NT 对账循环。
        raw = list(getattr(self, "_last_raw_positions", []))
        dispatch = self._settlement is not None and not self._settlement_inflight
        if dispatch:
            self._settlement_inflight = True
            self._loop.create_task(self._run_settlement(raw))
        # 低噪声验收/运维锚点:每次连续对账一条(生产约 5 分钟一条),确认 override 跑过 + venue 标活 + 结算派发决策。
        # 守卫 `_log`:离线单测经 `__new__` 绕过 NT init,`_log` 未初始化为 None;生产恒已注入。
        if self._log is not None:
            self._log.info(
                f"PM position reconcile OK: {len(reports)} report(s), "
                f"settlement {'dispatched' if dispatch else 'skipped'} ({len(raw)} raw positions)",
            )
        return reports
