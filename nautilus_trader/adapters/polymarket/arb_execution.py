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

from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.execution.session import ArbExecutionSessionMixin
from nautilus_trader.adapters.polymarket.settlement import PolymarketSettlement
from nautilus_trader.adapters.polymarket.settlement import SettlementPosition


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
        settlement: PolymarketSettlement | None = None,
        session_timeout_secs: float = 30.0,
    ) -> None:
        super().__init__(
            loop, http_client, msgbus, cache, clock,
            instrument_provider, ws_auth, config, name,
        )
        self._init_arb_session(
            session_timeout_secs=session_timeout_secs,
        )
        self._venue_liveness = venue_liveness
        self._settlement = settlement
        # #110:merge/redeem 改由 NT 连续 position 对账驱动(无 HealthCheckLoop)。
        # `_settlement_inflight` = single-flight 守卫(链上 tx 数秒,fire-and-forget 不阻塞对账循环,防并发重复提交)。
        self._settlement_inflight = False

    async def _submit_order(self, command) -> None:
        # #261:session 已由 mixin 的同步 `submit_order` 建立(派生态不能有空窗),此处不再建。
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
        `_cancel_residual_orders`/`_tracked_residual_cancel` 统一(撤单纳入 exec_count,exec_count→0 才清 in-flight)。
        `session_started=True`:cancel session 已由 base 同步预开,`_cancel_order` 不得再 begin(否则自撞、
        撤单在 begin 守卫处 return 而永不触达 venue);对齐 OE/SE `_cancel_one(session_started=True)`。"""
        cmd = CancelOrder(
            trader_id=self.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=order.venue_order_id,
            command_id=UUID4(),
            ts_init=self._clock.timestamp_ns(),
        )
        await self._cancel_order(cmd, session_started=True)

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

        try:
            report = await super().generate_order_status_report(command)
        except Exception as e:
            self._venue_liveness.mark_order_dead(POLYMARKET)  # #259:统一 mark_dead + raise(修订 #122)
            if self._log is not None:
                self._log.warning(f"PM order report query failed (mark dead, raise): {e!r}")
            raise
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

    async def generate_fill_reports(self, command) -> list:
        """#279:reconcile 不拉 trades API —— 恒返回 `[]`,对齐 OE/SE。

        PM 的 position 对账走 NT 原生 NET(`_reconcile_position_report_netting`:凭 `/positions`
        权威净仓 + `avg_px_open` 合成 inferred fill,不需要真 fill),因此 reconcile 完全不必碰
        trades API。上游会拉真 trades,但那会:① 启动时超时抛异常连坐掀翻整个 ExecutionMassStatus
        组装(丢弃已到手的权威 position);② 连续对账把持仓拖进"真 fill 挂历史母单(未 cache)→
        FillReport 早于 OrderStatusReport → 挂不上"的脆弱路径。返 `[]` 两头都避开,与 OE/SE
        `execution.py::generate_fill_reports` 同构。

        **live 成交不受影响**:PM 实时持仓由 USER WS trade(`_handle_user_trade_in_ws_trade_msg`
        → `generate_order_filled`)累加,与本方法无关;本方法仅供 reconcile / 从 fill 反建未知订单,
        后者正是要砍掉的脆弱源。详见 execution architecture §4.3bis (5d) / refactor #279。
        """
        return []

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
