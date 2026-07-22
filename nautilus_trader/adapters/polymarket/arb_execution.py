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

import asyncio

from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET
from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import GenerateOrderStatusReport
from nautilus_trader.execution.messages import QueryOrder

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
            venue_order_id = self._cache.venue_order_id(order.client_order_id)
            if venue_order_id is not None:
                self._log.warning(
                    "Polymarket submit result unknown; retaining SUBMITTED order for NT inflight query "
                    f"client_order_id={order.client_order_id}, venue_order_id={venue_order_id}: {e!r}",
                )
                return
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

    def _handle_ambiguous_submit_failure(self, order, reason: str | None) -> None:
        venue_order_id = self._cache.venue_order_id(order.client_order_id)
        self._log.warning(
            "Polymarket submit result unknown; retaining SUBMITTED order for NT inflight query "
            f"client_order_id={order.client_order_id}, venue_order_id={venue_order_id}, reason={reason!r}",
        )

    def _reserve_available_balance_for_accepted_order(self, event, sess: dict) -> None:
        """PM 关闭 accepted 预扣(#254):taker 单 accepted(MATCHED/MINED/CONFIRMED 任一
        先到达即 ack,见 execution.py `_handle_user_trade_in_ws_trade_msg`)后数秒内即由
        NT fill 增量记账扣减;预扣 + fill 增量会在下一轮 reconcile(~5min)覆盖前双扣可用
        余额,压住小账户的后续机会。OE/SE 保留预扣(挂单驻留时间长,且 venue 高频真值帧/
        响应很快覆盖本地估算)。"""

    async def _query_order(self, command: QueryOrder) -> None:
        """PM 卡在飞只查询一次；订单更新仍走 NT 通用 report 管道。"""
        self._venue_liveness.mark_order_dead(POLYMARKET)
        report_command = GenerateOrderStatusReport(
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            venue_order_id=command.venue_order_id,
            command_id=UUID4(),
            ts_init=self._clock.timestamp_ns(),
        )
        report = await self.generate_order_status_report(report_command, retry=False)
        if report is None:
            self._log.warning("PM in-flight order query returned no valid OrderStatusReport; order remains dead")
            return

        # MessageBus.send 同步调用 ExecEngine endpoint；返回后订单已走完通用 reconcile。
        self._send_order_status_report(report)
        self._venue_liveness.mark_order_alive(POLYMARKET)

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
            # 2026-07-10:collateral adapter 路径已直接产出 pUSD。先暂停主动 CLOB cache sync,
            # 由下一轮账户刷新验证是否还需要 update_balance_allowance(COLLATERAL)。
            # 如需恢复,取消下一行注释即可。
            # await self._sync_collateral_balance_allowance_after_settlement()
        finally:
            self._settlement_inflight = False

    async def _sync_collateral_balance_allowance_after_settlement(self) -> None:
        """merge/redeem 成功后同步 CLOB 余额缓存,不主动刷新 NT AccountState。"""
        from py_clob_client_v2 import AssetType
        from py_clob_client_v2 import BalanceAllowanceParams

        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=self._config.signature_type,
        )
        try:
            await asyncio.to_thread(self._http_client.update_balance_allowance, params)
        except Exception as exc:  # noqa: BLE001 — 下个 settlement/账户刷新周期可重试,不影响 liveness
            log = getattr(self, "_log", None)
            if log is not None:
                log.warning(
                    f"PM settlement succeeded but balance allowance sync failed: {exc!r}",
                )

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
            # #259:回归 NT 原生语义 —— mark_dead 后**重新抛出**(修订 #122 的"返空不 raise")。
            # NT 判定"venue 查询失败"的唯一通道是异常(`live/execution_engine.py:876`
            # `isinstance(reports_or_exception, Exception)` → `failed_venues`);返 [] 会被读成
            # "查询成功、venue 无挂单/无持仓",连续对账据此拿真实状态去对齐空报告。
            # `mark_*_dead` 只写我们自己的 VenueExecutionLiveness,NT 看不见,不能替代异常。
            self._venue_liveness.mark_order_dead(POLYMARKET)
            if self._log is not None:
                self._log.warning(f"PM order reports query failed (mark dead, raise): {e!r}")
            raise
        finally:
            self._retry_manager_pool = original_pool
        if recorder.failures:
            # RetryManager 内部吞掉的失败:同样是"查询失败"而非"无挂单",按 #259 抛出而非返空。
            self._venue_liveness.mark_order_dead(POLYMARKET)
            if self._log is not None:
                self._log.warning(f"PM order reports query failed (mark dead, raise): {recorder.failures!r}")
            raise RuntimeError(f"PM order status reports failed: {recorder.failures!r}")
        self._venue_liveness.mark_order_alive(POLYMARKET)
        return reports

    async def generate_order_status_report(self, command, *, retry: bool = True):
        if not retry:
            try:
                report = await super().generate_order_status_report(command, retry=False)
            except Exception as e:
                # #259:与复数方法统一为 mark_dead + raise(启动已不再被对账失败中止,见 ArbNautilusKernel)。
                # 注意区分:**查询失败**(抛)与 **venue 查无此单**(返 None,NT 契约合法值)是两回事。
                self._venue_liveness.mark_order_dead(POLYMARKET)
                if self._log is not None:
                    self._log.warning(f"PM one-shot order query failed (mark dead, raise): {e!r}")
                raise
            if report is None:
                self._venue_liveness.mark_order_dead(POLYMARKET)
            return report

        recorder = _RetryFailureRecorder(
            self._retry_manager_pool,
            {"generate_order_status_report"},
        )
        original_pool = self._retry_manager_pool
        self._retry_manager_pool = recorder
        try:
            report = await super().generate_order_status_report(command)
        except Exception as e:
            self._venue_liveness.mark_order_dead(POLYMARKET)  # #259:统一 mark_dead + raise(修订 #122)
            if self._log is not None:
                self._log.warning(f"PM order report query failed (mark dead, raise): {e!r}")
            raise
        finally:
            self._retry_manager_pool = original_pool
        if recorder.failures:
            self._venue_liveness.mark_order_dead(POLYMARKET)
            if self._log is not None:
                self._log.warning(f"PM order report query failed (mark dead, raise): {recorder.failures!r}")
            raise RuntimeError(f"PM order status report failed: {recorder.failures!r}")
        if report is None:
            self._venue_liveness.mark_order_dead(POLYMARKET)
            return None
        self._venue_liveness.mark_order_alive(POLYMARKET)
        return report

    async def generate_position_status_reports(self, command):
        try:
            reports = await super().generate_position_status_reports(command)  # 单次拉 /positions,上游 stash raw
        except Exception as e:
            # #259:回归 NT 原生语义 —— mark_dead 后**重新抛出**(修订 #122)。返 [] 会让
            # `_query_position_status_reports` 把 PM 当成"查询成功、无持仓",于是
            # `_did_position_status_query_fail` 恒 False、跳过保护失效,连续对账拿
            # `_create_flat_position_report`(qty=0)当目标,合成 SELL 抹掉真实持仓的账面记录。
            self._venue_liveness.mark_position_dead(POLYMARKET)
            if self._log is not None:
                self._log.warning(f"PM position reports query failed (mark dead, raise): {e!r}")
            raise
        self._venue_liveness.mark_position_alive(POLYMARKET)
        # #110:同一次拉的原始 /positions 跑 merge/redeem —— fire-and-forget + single-flight,不阻塞 NT 对账循环。
        raw = list(getattr(self, "_last_raw_positions", []))
        dispatch = self._settlement is not None and not self._settlement_inflight
        if dispatch:
            self._settlement_inflight = True
            self._loop.create_task(self._run_settlement(raw))
        await self._refresh_account_state_after_position_reconcile()
        # 低噪声验收/运维锚点:每次连续对账一条(生产约 5 分钟一条),确认 override 跑过 + venue 标活 + 结算派发决策。
        # 守卫 `_log`:离线单测经 `__new__` 绕过 NT init,`_log` 未初始化为 None;生产恒已注入。
        if self._log is not None:
            self._log.info(
                f"PM position reconcile OK: {len(reports)} report(s), "
                f"settlement {'dispatched' if dispatch else 'skipped'} ({len(raw)} raw positions)",
            )
        return reports

    async def _refresh_account_state_after_position_reconcile(self) -> None:
        """PM position reconciliation 成功后刷新账户可用余额。

        余额刷新失败不改变 position liveness:position reports 已成功,余额下轮再试。
        """
        try:
            await self._update_account_state()
        except Exception as e:  # noqa: BLE001 — 余额刷新不能让 position reconcile 失败
            log = getattr(self, "_log", None)
            if log is not None:
                log.warning(f"PM balance refresh after position reconcile failed: {e!r}")
