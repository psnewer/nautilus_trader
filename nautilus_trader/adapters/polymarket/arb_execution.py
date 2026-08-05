"""
ArbPolymarketExecutionClient —— PM 执行客户端薄子类(Q18c 宿主层)。

详细设计:`docs/arbitrage/architectures/execution/architecture.md §3.1 / §4.3 / §4.6`。

= 上游 `PolymarketExecutionClient`(订单 IO / 账户状态 / reports,直接复用)
  + `ArbExecutionSessionMixin`(session / 超时 / execution.* 同步)
  + `PolymarketSettlement`(#110:merge/redeem,由 **NT 连续 position 对账** 在返回仓位报告前触发;
    **无 HealthCheckLoop** —— PM 健康检查已退役,对齐 OE #109)。

**位置(refactor.md #33 校准)**:本类是 PM venue-coupled 代码 → 住 PM adapter 目录(P9 唯一
例外:venue 适配器放 `nautilus_trader/adapters/<venue>/`)。**与上游 `execution.py` 同目录但不
同文件**(`arb_execution.py`)避免 upstream merge 冲突;import 上游类直接子类化。

**验证边界**:真 `ClobClient`/`ws_auth`/Data API 仍靠实盘;离线覆盖纯映射
`pm_raw_position_to_settlement` 以及 reports override 的 liveness / settlement 两阶段接线。
"""

from __future__ import annotations

import asyncio

import msgspec
from py_clob_client_v2 import MarketOrderArgs
from py_clob_client_v2 import PartialCreateOrderOptions
from py_clob_client_v2.clob_types import OrderType as PolyOrderType

from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient
from nautilus_trader.adapters.polymarket.settlement import PolymarketSettlement
from nautilus_trader.adapters.polymarket.settlement import SettlementPosition
from nautilus_trader.adapters.polymarket.settlement import SettlementResult
from nautilus_trader.core.nautilus_pyo3 import HttpResponse
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import GenerateOrderStatusReport
from nautilus_trader.execution.messages import QueryOrder
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import order_side_to_str
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from src.arbitrage.common.opportunity import meta_from_order
from src.arbitrage.common.realized_pnl import RealizedPnlLedger
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.execution.session import ArbExecutionSessionMixin
from src.arbitrage.execution.session import cancel_session_started


def _optional_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _realized_by_instrument(rows: list[dict]) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in rows:
        condition_id = str(row.get("conditionId", "") or "")
        asset_id = str(row.get("asset", "") or "")
        if not condition_id or not asset_id:
            continue
        instrument_id = f"{condition_id}-{asset_id}.POLYMARKET"
        realized_pnl = _optional_float(row.get("realizedPnl", 0.0))
        if realized_pnl is None:
            continue
        result[instrument_id] = result.get(instrument_id, 0.0) + realized_pnl
    return result


def _native_realized_for_instrument(cache, instrument_id: str, account_id) -> float:
    total = 0.0
    for position in cache.positions(
        instrument_id=InstrumentId.from_str(instrument_id),
        account_id=account_id,
    ):
        pnl = getattr(position, "realized_pnl", None)
        if pnl is None:
            continue
        total += pnl.as_double() if hasattr(pnl, "as_double") else float(pnl)
    return total


def pm_raw_position_to_settlement(item: dict) -> SettlementPosition:
    """#110:PM Data API `/positions` **原始 dict** → settlement 视图(纯映射,可单测)。
    键名按 Data API:`conditionId` / `size` / `negativeRisk` / `redeemable`。"""
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
        realized_pnl_ledger: RealizedPnlLedger | None = None,
        session_timeout_secs: float = 30.0,
        market_order_enabled: bool = False,
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
        self._realized_pnl_ledger = realized_pnl_ledger
        self._market_order_enabled = bool(market_order_enabled)
        # #110/#283/#285:merge/redeem 由 NT 连续 position 对账驱动；尝试过 merge 后
        # 同轮重拉 positions，避免交易结果不确定时向 NT 返回 merge 前的旧仓位。
        self._settlement_inflight = False

    def _should_book_early_fill(self, venue_order_id) -> bool:
        # 套利主单 `enable_timeout=false`(tracking ends on ack):这类单常被 spread_cancel_recovery
        # 立刻撤,若等 CONFIRMED 才记账,cancel/match race 会丢掉真实成交(MATCHED 到 → leaves
        # 被 cancel 确认置 CANCELED → CONFIRMED 到时被 is_closed 拦)。改在 MATCHED 就记账:此刻单
        # 多半仍 open,走正常 OrderFilled → PARTIALLY_FILLED,随后真实 cancel 确认把它收成 CANCELED
        # (带真实 filled_qty)。后续 MINED/CONFIRMED 由 per-fill 去重挡掉,不会重复记。
        client_order_id = self._cache.client_order_id(venue_order_id)
        order = self._cache.order(client_order_id) if client_order_id is not None else None
        if order is None:
            return False
        meta = meta_from_order(order)
        return meta is not None and meta.enable_timeout is False

    async def _cancel_order(self, command: CancelOrder, *, session_started: bool = False) -> None:
        session_started = session_started or cancel_session_started(command)
        await super()._cancel_order(command, session_started=session_started)

    async def _submit_limit_order(self, command, instrument) -> None:
        """按 execution 开关在最终 PM 出站边界选择普通限价或官方市价单。"""
        if not self._market_order_enabled:
            await super()._submit_limit_order(command, instrument)
            return

        order = command.order
        amount = (
            float(order.quantity) * float(order.price)
            if order.side == OrderSide.BUY
            else float(order.quantity)
        )
        order_args = MarketOrderArgs(
            token_id=get_polymarket_token_id(order.instrument_id),
            amount=amount,
            side=order_side_to_str(order.side),
            order_type=PolyOrderType.FOK,
        )
        neg_risk = self._get_neg_risk_for_instrument(instrument)
        options = PartialCreateOrderOptions(neg_risk=neg_risk)
        signed_order = await asyncio.to_thread(
            self._http_client.create_market_order,
            order_args,
            options=options,
        )

        self._register_signed_order_id(order, signed_order, neg_risk=neg_risk)
        self.generate_order_submitted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )

        base_quantity = None
        if order.side == OrderSide.BUY:
            taker_amount = int(signed_order.takerAmount)
            base_quantity = Quantity(taker_amount / 1e6, instrument.size_precision)

        await self._post_signed_order(
            order,
            signed_order,
            order_type_override=PolyOrderType.FOK,
            base_quantity=base_quantity,
        )

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
        # in-flight 查询暂不参与 venue liveness；保留调用位置供以后按需恢复。
        # self._venue_liveness.mark_order_dead(POLYMARKET)
        report_command = GenerateOrderStatusReport(
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            venue_order_id=command.venue_order_id,
            command_id=UUID4(),
            ts_init=self._clock.timestamp_ns(),
        )
        report = await self.generate_order_status_report(report_command, retry=False)
        if report is None:
            self._log.warning("PM in-flight order query returned no valid OrderStatusReport")
            return

        # MessageBus.send 同步调用 ExecEngine endpoint；返回后订单已走完通用 reconcile。
        self._send_order_status_report(report)
        # self._venue_liveness.mark_order_alive(POLYMARKET)

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

    async def _run_settlement(self, raw_positions: list) -> SettlementResult:
        """执行 merge/redeem；tx 失败仅记入结果，`finally` 清 single-flight 守卫。"""
        try:
            return await self._settlement.run(
                [pm_raw_position_to_settlement(p) for p in raw_positions],
            )
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
        snapshot = self._capture_reconciliation_state_snapshot(kind="order")
        try:
            reports = await super().generate_order_status_reports(command)
        except Exception as e:
            # #259:回归 NT 原生语义 —— **重新抛出**(修订 #122 的"返空不 raise")。
            # NT 判定"venue 查询失败"的唯一通道是异常(`live/execution_engine.py:876`
            # `isinstance(reports_or_exception, Exception)` → `failed_venues`);返 [] 会被读成
            # "查询成功、venue 无挂单/无持仓",连续对账据此拿真实状态去对齐空报告。
            # `mark_*_dead` 只写我们自己的 VenueExecutionLiveness,NT 看不见,不能替代异常。
            if self._log is not None:
                self._log.warning(f"PM order reports query failed (raise): {e!r}")
            raise
        return self._guard_reconciliation_reports("order", reports, snapshot)

    async def generate_order_status_report(self, command, *, retry: bool = True):
        # #319:单数=NT inflight-check / QueryOrder 解析专用路径,**不附 snapshot**、豁免 staleness 闸。
        # 该路径的天职是拿 venue 真实态解析 INFLIGHT 单,往返期间状态在变本是常态;附 snapshot 会把
        # 「订单存活」的解析报告判 stale 丢掉 → 单子悬到超时被误置 REJECTED。回归由 NT 状态机兜底。
        if not retry:
            try:
                report = await super().generate_order_status_report(command, retry=False)
            except Exception as e:
                # #259:查询失败抛出；liveness 由调用方按查询结果统一维护。
                # 注意区分:**查询失败**(抛)与 **venue 查无此单**(返 None,NT 契约合法值)是两回事。
                if self._log is not None:
                    self._log.warning(f"PM one-shot order query failed (raise): {e!r}")
                raise
            return report

        try:
            report = await super().generate_order_status_report(command)
        except Exception as e:
            if self._log is not None:
                self._log.warning(f"PM order report query failed (raise): {e!r}")
            raise
        return report

    async def generate_position_status_reports(self, command):
        snapshot = self._capture_reconciliation_state_snapshot(kind="position")
        try:
            reports = await super().generate_position_status_reports(command)  # 单次拉 /positions,上游 stash raw
        except Exception as e:
            # #259:回归 NT 原生语义 —— **重新抛出**(修订 #122)。返 [] 会让
            # `_query_position_status_reports` 把 PM 当成"查询成功、无持仓",于是
            # `_did_position_status_query_fail` 恒 False、跳过保护失效,连续对账拿
            # `_create_flat_position_report`(qty=0)当目标,合成 SELL 抹掉真实持仓的账面记录。
            if self._log is not None:
                self._log.warning(f"PM position reports query failed (raise): {e!r}")
            raise
        raw = list(getattr(self, "_last_raw_positions", []))
        merge_refreshed = False
        if self._settlement is not None and not self._settlement_inflight:
            self._settlement_inflight = True
            result = await self._run_settlement(raw)
            if result.merges:
                try:
                    reports = await super().generate_position_status_reports(command)
                except Exception as e:
                    if self._log is not None:
                        self._log.warning(
                            f"PM post-merge position reports query failed (raise): {e!r}",
                        )
                    raise
                raw = list(getattr(self, "_last_raw_positions", []))
                merge_refreshed = True

        realized_snapshot = await self._load_realized_pnl_snapshot(raw)
        await self._refresh_account_state_after_position_reconcile()
        # 低噪声验收/运维锚点:每次连续对账一条(生产约 5 分钟一条),确认 override 跑过 + 结算结果。
        # 守卫 `_log`:离线单测经 `__new__` 绕过 NT init,`_log` 未初始化为 None;生产恒已注入。
        if self._log is not None:
            self._log.info(
                f"PM position reconcile OK: {len(reports)} report(s), "
                f"settlement {'merge-refreshed' if merge_refreshed else 'checked'} "
                f"({len(raw)} raw positions)",
            )
        return self._guard_reconciliation_reports(
            "position",
            reports,
            snapshot,
            payload=realized_snapshot,
        )

    def apply_reconciliation_batch(self, kind: str, batch, applied_instruments=None) -> None:
        if kind == "position":
            self._commit_realized_pnl_snapshot(batch.payload, applied_instruments)

    async def _load_realized_pnl_snapshot(
        self,
        current_positions: list[dict],
    ) -> dict[str, float] | None:
        """拉取权威 realized 候选；最终状态校验前不得修改共享账本。"""
        ledger = getattr(self, "_realized_pnl_ledger", None)
        if ledger is None:
            return None
        try:
            closed_positions = await self._fetch_closed_positions()
        except Exception as exc:  # 保留上一份完整基线，不污染 position reconcile
            self._log.warning(f"PM realized PnL reconcile skipped: closed positions query failed: {exc!r}")
            return None

        return _realized_by_instrument([*current_positions, *closed_positions])

    def _commit_realized_pnl_snapshot(self, external: dict[str, float] | None, applied_instruments=None) -> None:
        ledger = getattr(self, "_realized_pnl_ledger", None)
        if ledger is None or external is None:
            return
        # #318:per-pair 选择性 —— 只更新通过校验的 instrument 的 offset;native 需覆盖 external 与被选集合。
        only = None if applied_instruments is None else {str(i) for i in applied_instruments}
        instruments = set(external) if only is None else (set(external) | only)
        native = {
            instrument_id: _native_realized_for_instrument(
                self._cache,
                instrument_id,
                self.account_id,
            )
            for instrument_id in instruments
        }
        ledger.replace_instrument_snapshot(
            self.account_id,
            external_realized=external,
            native_realized=native,
            only_instruments=only,
        )

    async def _fetch_closed_positions(self, *, limit: int = 50) -> list[dict]:
        base_url = (self._config.base_url_data_api or "https://data-api.polymarket.com").rstrip("/")
        url = f"{base_url}/closed-positions"
        results: list[dict] = []
        offset = 0
        while True:
            response: HttpResponse = await self._http_client_async.get(
                url=url,
                params={
                    "user": self._user_address,
                    "limit": str(limit),
                    "offset": str(offset),
                    "sortBy": "TIMESTAMP",
                    "sortDirection": "DESC",
                },
            )
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}: Failed to fetch closed positions")
            page = msgspec.json.decode(response.body)
            if not isinstance(page, list) or not page:
                break
            results.extend(page)
            if len(page) < limit:
                break
            offset += limit
            if offset > 100000:
                raise RuntimeError("Closed positions offset exceeded 100000")
        return results

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
