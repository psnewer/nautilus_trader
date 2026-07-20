"""SharpExch execution adapter。

第一阶段按 OE 型 venue 结构接入 NT runtime;place/cancel 与成交路径均已由独立真单 probe 验证。
"""

from __future__ import annotations

import asyncio
import random
import string
import time
from dataclasses import dataclass

from nautilus_trader.adapters.sharpexch.message_parser import SharpExchMessageParser
from nautilus_trader.adapters.sharpexch.web import SharpExchLoginState
from nautilus_trader.adapters.sharpexch.web import se_login
from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import AccountBalance
from nautilus_trader.model.objects import Money

from src.arbitrage.common.control import TOPIC_ARBITRAGE_PARAMS
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.execution.session import ArbExecutionSessionMixin


SHARPEXCH = "SHARPEXCH"
_EXEC_WS_IDLE_TIMEOUT_SECS = 300.0
_CONNECT_READY_TIMEOUT_SECS = 30.0
_ORDER_IO_TIMEOUT_SECS = 5.0


def _se_result_is_transport_unknown(result) -> bool:
    if not isinstance(result, dict):
        return False
    response = result.get("venue_response")
    return bool(result.get("transport_unknown")) or (
        isinstance(response, dict) and bool(response.get("_transport_error"))
    )


_FX_AMOUNT_KEYS = {
    "liability",
    "netprofit",
    "profitnet",
    "profitgross",
    "profit",
    "pnl",
}


@dataclass(frozen=True)
class SharpExchLegacyOrder:
    """SE executor 前置订单表示。

    当前不复用 `src.arbitrage.common.order_models.Venue`,避免在 SE 未接 runtime 前改共享 enum。
    """

    venue: str
    market_id: str
    selection_id: str
    handicap: float
    side: str
    price: float
    size: float
    order_type: str = "GTC"


class SharpExchExecutionClient(ArbExecutionSessionMixin, LiveExecutionClient):
    """SE execution client。

    负责 SE 登录、general WS 业务帧、NT order submit/cancel 到 SE place/cancel API 的边界适配。
    """

    def __init__(
        self,
        loop,
        browser_manager,
        msgbus,
        cache,
        clock,
        instrument_provider,
        config,
        *,
        venue_liveness: VenueExecutionLiveness | None = None,
        pair_registry: PairRegistry | None = None,
        pair_inflight=None,
        session_timeout_secs: float = 30.0,
        fx: float = 1.0,
        browser_lock: asyncio.Lock | None = None,
        login_state: SharpExchLoginState | None = None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(SHARPEXCH),
            venue=Venue(SHARPEXCH),
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            base_currency=USD,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._init_arb_session(
            session_timeout_secs=session_timeout_secs,
            pair_registry=pair_registry,
            pair_inflight=pair_inflight,
        )
        self._set_account_id(AccountId(f"{SHARPEXCH}-001"))
        self._browser_manager = browser_manager
        self._config = config
        self._parser = SharpExchMessageParser()
        self._venue_liveness = venue_liveness
        self._fx = float(fx) if fx > 0 else 1.0
        from nautilus_trader.adapters.sharpexch.executor import SharpExchExecutor

        self._executor = SharpExchExecutor(fx_getter=self._current_fx)
        self._page = None
        self._ws_handler = None
        self._page_lock = asyncio.Lock()
        self._order_io_timeout_secs = _ORDER_IO_TIMEOUT_SECS
        self._browser_lock = browser_lock or asyncio.Lock()
        self._login_state = login_state
        self._bet_fill_seq: dict[str, int] = {}
        self._current_bets: dict[str, dict] = {}
        self._last_current_bets_ns = 0
        self._last_frame_ns = 0
        self._exec_ws_idle_timeout_ns = secs_to_nanos(_EXEC_WS_IDLE_TIMEOUT_SECS)
        self._reload_inflight = None
        # 从 reload 发起起算，导航与 CURRENT_BETS 重推共用页面加载总预算。
        self._reload_bets_wait_ns = secs_to_nanos(self._config.page_timeout / 1000.0)
        # #255:reload 后首帧 = 静默对账帧(不自派生 fill / 不推报告 / 不标 alive);
        # 报告与 alive 由触发 reload 的调用方负责(QueryOrder 推送 / NT 拉取返回)。
        self._reload_frame_pending = False
        self._current_bets_frames_seen = 0
        self._balance_seen = False
        self._last_balance_value: float | None = None
        self._balance_ready_fut = None
        self._current_bets_ready_fut = None
        self._connect_ready_timeout_secs = _CONNECT_READY_TIMEOUT_SECS
        self._msgbus.subscribe(topic=TOPIC_ARBITRAGE_PARAMS, handler=self._on_set_arbitrage_params_cmd)

    async def _connect(self) -> None:
        """建立 SE execution page 与 general WS 监听。

        Data/Exec 可能共用同一 browser context,登录/API 初始化需要经共享 browser lock 串行化。
        """

        from nautilus_trader.adapters.sharpexch.websocket_handler import SharpExchWebSocketHandler

        await self._browser_manager.start()
        self._page = await self._browser_manager.create_page("execution")
        self._page.set_default_timeout(self._config.page_timeout)

        # Playwright 的 websocket 监听只能捕获注册之后创建的 WS,必须先挂 handler 再导航。
        self._ws_handler = SharpExchWebSocketHandler(self._page, logger=self._log)
        self._ws_handler.on_order_update(self._on_general_frame)
        self._ws_handler.on_frame(self._mark_exec_frame)
        await self._ws_handler.start()

        # response 监听必须在 login(即首次导航)之前注册,才能捕获 SPA 初始化时发出的
        # profile/balance API 请求。
        self._page.on("response", self._on_response)

        # se_login 内部使用 browser_lock 串行化登录,避免并发登录触发 Cloudflare
        loop = asyncio.get_running_loop()
        self._balance_ready_fut = loop.create_future()
        self._current_bets_ready_fut = loop.create_future()
        await self._login()

        # 连接就绪信号:profile/balance API + execution general WS CURRENT_BETS。
        # 有限等待只用于避免 startup 过早进入 reconcile;超时后仍 fail-soft 交后续 reconnect/reconcile 自愈。
        await self._wait_for_initial_business_state()
        if not self._balance_seen:
            self._log.warning("SE balance not captured from responses; reporting 0.0")
            self.generate_account_state(
                balances=se_balance_to_account_balances(0.0),
                margins=[],
                reported=True,
                ts_event=self._clock.timestamp_ns(),
            )
        self._log.info("SharpExchExecutionClient connected")

    async def _wait_for_initial_business_state(self) -> None:
        futures = [f for f in (self._balance_ready_fut, self._current_bets_ready_fut) if f is not None]
        if not futures:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*futures),
                timeout=self._connect_ready_timeout_secs,
            )
        except asyncio.TimeoutError:
            missing = []
            if not self._balance_seen:
                missing.append("profile_balance")
            if self._last_current_bets_ns <= 0:
                missing.append("CURRENT_BETS")
            if missing:
                self._log.warning(
                    f"SE execution initial business state timeout: missing={','.join(missing)}",
                )
        finally:
            self._balance_ready_fut = None
            self._current_bets_ready_fut = None

    async def _login(self) -> None:
        """SE 登录页操作。

        SE 外层登录页提交用户名/密码后,真实 customer app 位于 portal iframe。
        browser context 级登录状态串行化登录,避免 Data/Exec 并发登录与二次提交旧登录页。
        """

        await se_login(self._page, self._config, self._browser_lock, login_state=self._login_state)
        self._log.info("SharpExch login successful")

    async def _disconnect(self) -> None:
        if self._page is not None:
            try:
                self._page.remove_listener("response", self._on_response)
            except Exception:  # noqa: BLE001
                pass
        if self._ws_handler is not None:
            await self._ws_handler.stop()
            self._ws_handler = None
        self._page = None

    def _current_fx(self) -> float:
        return 1.0

    async def _on_response(self, response) -> None:
        """response 监听器: 从登录后 SPA 的 profile/balance API 响应中提取余额。"""
        url = response.url
        # 只看 API 请求(排除静态资源)
        if "/api/" not in url:
            return
        # 排除已知的非余额 API(placeBets/cancelBets/competition 等)
        if any(k in url for k in ("placeBets", "cancelBets", "competition", "market-prices")):
            return
        try:
            body = await response.json()
        except Exception:  # noqa: BLE001
            return
        if not isinstance(body, dict):
            return
        balance_val = _extract_balance_from_response(body)
        if balance_val is not None:
            if self._last_balance_value is not None and abs(balance_val - self._last_balance_value) < 1e-9:
                self._balance_seen = True
                _resolve_future(self._balance_ready_fut)
                return
            self._log.info("SE balance from %s: %.2f" % (url, balance_val))
            self.generate_account_state(
                balances=se_balance_to_account_balances(balance_val),
                margins=[],
                reported=True,
                ts_event=self._clock.timestamp_ns(),
            )
            self._balance_seen = True
            self._last_balance_value = balance_val
            _resolve_future(self._balance_ready_fut)

    def _mark_exec_frame(self) -> None:
        self._last_frame_ns = self._clock.timestamp_ns()

    def _exec_ws_fresh(self) -> bool:
        return (
            self._last_frame_ns > 0
            and self._clock.timestamp_ns() - self._last_frame_ns < self._exec_ws_idle_timeout_ns
        )

    def _on_set_arbitrage_params_cmd(self, cmd) -> None:
        fx = getattr(cmd, "fx", None)
        if fx is None:
            return
        fx = float(fx)
        if fx <= 0:
            self._log.warning(f"Ignore invalid SE fx={fx}")
            return
        self._fx = fx

    async def _submit_order(self, command) -> None:
        if not self._begin_session(command):
            return
        order = command.order
        try:
            result = await self._place_via_executor(order)
            if result is None or not result.get("success"):
                if result is not None and _se_result_is_transport_unknown(result):
                    self._log.warning(
                        "SE submit result unknown; retaining SUBMITTED order for NT inflight reconcile "
                        f"client_order_id={order.client_order_id}: {result.get('message')}",
                    )
                    return
                reason = (result or {}).get("message") or "submit failed"
                self.generate_order_rejected(
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    reason=reason,
                    ts_event=self._clock.timestamp_ns(),
                )
                return
            from nautilus_trader.model.identifiers import VenueOrderId

            venue_order_id = result.get("venue_order_id") or order.client_order_id.value
            self.generate_order_accepted(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=VenueOrderId(str(venue_order_id)),
                ts_event=self._clock.timestamp_ns(),
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "SE submit result unknown; retaining SUBMITTED order for NT inflight reconcile "
                f"client_order_id={order.client_order_id}: {exc!r}",
            )

    async def _place_via_executor(self, nt_order):
        inst = self._cache.instrument(nt_order.instrument_id)
        if inst is None:
            self._log.error(f"SE place: instrument {nt_order.instrument_id} not in cache")
            return None
        legacy = nt_order_to_legacy_order(nt_order, inst)
        if legacy is None:
            self._log.error(f"SE place: {nt_order.instrument_id} missing market_id/selection_id")
            return None
        if self._executor is None:
            self._log.error("SE place: executor is not initialized")
            return None
        self.generate_order_submitted(
            strategy_id=nt_order.strategy_id,
            instrument_id=nt_order.instrument_id,
            client_order_id=nt_order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )
        return await asyncio.wait_for(
            self._run_page_write(lambda: self._executor.place_order(legacy, self._page)),
            timeout=self._order_io_timeout_secs,
        )

    async def _cancel_order(self, command) -> None:
        await self._cancel_one(
            command.strategy_id,
            command.instrument_id,
            command.client_order_id,
            command.venue_order_id,
        )

    async def _cancel_residual_one(self, order) -> None:
        """撤一条 cancel-only 残单。

        session 计数由 `ArbExecutionSessionMixin._tracked_residual_cancel` 统一收尾;
        本方法只负责把残留 NT order 复用正常撤单路径送到 SE。
        """

        await self._cancel_one(
            order.strategy_id,
            order.instrument_id,
            order.client_order_id,
            order.venue_order_id,
            session_started=True,
        )

    async def _cancel_one(
        self,
        strategy_id,
        instrument_id,
        client_order_id,
        venue_order_id,
        *,
        session_started: bool = False,
    ) -> None:
        nt_order = self._cache.order(client_order_id)
        voi = venue_order_id or (nt_order.venue_order_id if nt_order is not None else None)
        now = self._clock.timestamp_ns()
        if nt_order is not None and not session_started and not self._begin_cancel_session(nt_order):
            return
        if self._executor is None or voi is None:
            self.generate_order_cancel_rejected(
                strategy_id,
                instrument_id,
                client_order_id,
                voi,
                "no executor / venue_order_id",
                now,
            )
            return
        inst = self._cache.instrument(instrument_id)
        bet = self._current_bets.get(str(voi)) or {}
        market_id = str(getattr(inst, "market_id", "") or bet.get("marketId", ""))
        try:
            result = await asyncio.wait_for(
                self._run_page_write(
                    lambda: self._executor.cancel_order(market_id, str(voi), self._page, bet=bet),
                ),
                timeout=self._order_io_timeout_secs,
            )
        except Exception as exc:  # noqa: BLE001 — 结果未知,保留 PENDING_CANCEL 给 NT inflight reconcile
            self._log.warning(
                "SE cancel result unknown; retaining PENDING_CANCEL order for NT inflight reconcile "
                f"client_order_id={client_order_id}, venue_order_id={voi}: {exc!r}",
            )
            return
        if result is not None and result.get("success"):
            self._log.info(
                f"SE cancel request accepted: client_order_id={client_order_id}, "
                f"venue_order_id={voi}; awaiting CURRENT_BETS confirmation",
            )
        else:
            if result is not None and _se_result_is_transport_unknown(result):
                self._log.warning(
                    "SE cancel result unknown; retaining PENDING_CANCEL order for NT inflight reconcile "
                    f"client_order_id={client_order_id}, venue_order_id={voi}: {result.get('message')}",
                )
                return
            reason = (result or {}).get("message") or "cancel failed"
            self.generate_order_cancel_rejected(strategy_id, instrument_id, client_order_id, voi, reason, now)

    async def _query_order(self, command) -> None:
        """卡在飞:dead → 强制 reload(静默帧)→ 本方法推送全量报告对账 → alive(#255)。"""
        if self._venue_liveness is not None:
            self._venue_liveness.mark_order_dead(SHARPEXCH)
            self._venue_liveness.mark_position_dead(SHARPEXCH)
        if not await self._ensure_exec_snapshot_fresh(force=True):
            return  # reload 失败 / CURRENT_BETS 未重推 → 保持 dead
        self._push_reports_from_snapshot()
        if self._venue_liveness is not None:
            self._venue_liveness.mark_order_alive(SHARPEXCH)
            self._venue_liveness.mark_position_alive(SHARPEXCH)

    async def _run_page_write(self, operation):
        async with self._page_lock:
            return await operation()

    async def _modify_order(self, command) -> None:
        order = self._cache.order(command.client_order_id)
        venue_order_id = command.venue_order_id or (order.venue_order_id if order is not None else None)
        self.generate_order_modify_rejected(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            venue_order_id=venue_order_id,
            reason="SharpExch does not support order modification",
            ts_event=self._clock.timestamp_ns(),
        )

    async def _reload_exec_page(self) -> bool:
        if self._page is None:
            return False
        # #255:reload 皆对账 —— 重推的首帧必须静默(报告消费者=触发方);失败时标记不清,
        # 顺延到下一自然帧(该帧成交由再下一帧的累计差分自愈)。
        self._reload_frame_pending = True
        reload_ts = self._clock.timestamp_ns()
        try:
            async with self._page_lock:
                await self._page.reload(wait_until="domcontentloaded", timeout=self._config.page_timeout)
        except Exception as exc:  # noqa: BLE001
            self._log.warning(f"SE exec page reload failed: {exc!r}")
            return False
        while self._last_current_bets_ns <= reload_ts:
            if self._clock.timestamp_ns() - reload_ts > self._reload_bets_wait_ns:
                self._log.warning("SE exec reload: CURRENT_BETS not repushed before timeout")
                return False
            await asyncio.sleep(0.1)
        return True

    async def _ensure_exec_snapshot_fresh(self, *, force: bool = False) -> bool:
        if not force and self._last_current_bets_ns > 0 and self._exec_ws_fresh():
            return True
        if self._reload_inflight is not None:
            return await self._reload_inflight
        self._reload_inflight = asyncio.ensure_future(self._reload_exec_page())
        try:
            return await self._reload_inflight
        finally:
            self._reload_inflight = None

    def _on_general_frame(self, message) -> None:
        parsed = self._parser.parse_general_frame(message)
        if parsed is None:
            return
        # WS BALANCE 帧不再消费(实测返回 0.00 不可靠),余额改从 HTTP 响应拦截
        if parsed["type"] == "current_bets":
            self._on_current_bets(parsed["bets"])

    def _on_current_bets(self, bets) -> None:
        from nautilus_trader.model.enums import LiquiditySide
        from nautilus_trader.model.identifiers import TradeId
        from nautilus_trader.model.identifiers import VenueOrderId

        usd_bets = normalize_current_bets_to_usd(bets, 1.0)
        self._current_bets = {str(b.get("offerId", "")): b for b in usd_bets if b.get("offerId")}
        self._current_bets_frames_seen += 1
        if self._current_bets_frames_seen == 1:
            self._log.info(f"SE CURRENT_BETS routed: bets={len(self._current_bets)}")
        self._emit_cancel_events_from_current_bets()

        # #255:reload 后首帧是静默对账帧(同 OE)——不自派生 fill,成交由触发 reload 的
        # 调用方经报告对账(inferred fill)补齐;同帧双路派生会因 fill 事件异步 apply
        # 双计部分成交(2026-07-18 实盘 overfill 根因)。常规帧只走事件路径,不推报告。
        reload_frame = self._reload_frame_pending
        if reload_frame:
            self._reload_frame_pending = False
        else:
            for fill in current_bets_to_fills(bets):
                offer_id = fill["offer_id"]
                voi = VenueOrderId(offer_id)
                client_order_id = self._cache.client_order_id(voi)
                nt_order = self._cache.order(client_order_id) if client_order_id is not None else None
                if nt_order is None:
                    self._log.warning(f"CURRENT_BETS fill offerId={offer_id} has no NT order; skip")
                    continue
                inst = self._cache.instrument(nt_order.instrument_id)
                if inst is None:
                    continue
                matched_qty = fill["size_matched"]
                already_filled = nt_order.filled_qty.as_double()
                remaining_qty = nt_order.quantity.as_double() - already_filled
                last_qty = min(matched_qty - already_filled, remaining_qty)
                if last_qty <= 0:
                    continue
                seq = self._bet_fill_seq.get(offer_id, 0) + 1
                self._bet_fill_seq[offer_id] = seq
                self.generate_order_filled(
                    strategy_id=nt_order.strategy_id,
                    instrument_id=nt_order.instrument_id,
                    client_order_id=client_order_id,
                    venue_order_id=voi,
                    venue_position_id=None,
                    trade_id=TradeId(f"SE-{offer_id}-{seq}"),
                    order_side=nt_order.side,
                    order_type=nt_order.order_type,
                    last_qty=inst.make_qty(last_qty),
                    last_px=inst.make_price(fill["avg_price"]),
                    quote_currency=USD,
                    commission=Money(0, USD),
                    liquidity_side=LiquiditySide.MAKER,
                    ts_event=self._clock.timestamp_ns(),
                )

        self._last_current_bets_ns = self._clock.timestamp_ns()
        _resolve_future(self._current_bets_ready_fut)
        # 静默帧不标 alive:恢复语境下 alive 必须晚于报告进入 ExecEngine(#244 顺序),由触发方标。
        if not reload_frame and self._venue_liveness is not None:
            self._venue_liveness.mark_order_alive(SHARPEXCH)
            self._venue_liveness.mark_position_alive(SHARPEXCH)

    def _push_reports_from_snapshot(self) -> None:
        """全量 order/position 报告 → ExecEngine 对账;QueryOrder 恢复通路专用(#255)。"""
        for bet in self._current_bets.values():
            report = self._build_order_report(bet)
            if report is not None:
                self._send_order_status_report(report)
        for report in self._build_position_status_reports_from_current_bets():
            self._send_position_status_report(report)

    @staticmethod
    def _bet_is_cancelled(bet: dict) -> bool:
        state = bet.get("offerState") or bet.get("offer_state") or bet.get("state") or bet.get("status")
        return str(state or "").upper() in {"CANCELLED", "CANCELED"}

    def _emit_cancel_events_from_current_bets(self) -> None:
        from nautilus_trader.model.identifiers import VenueOrderId

        for client_order_id, sess in self._active_cancel_sessions_snapshot():
            venue_order_id = sess.get("venue_order_id")
            nt_order = self._cache.order(client_order_id)
            if venue_order_id is None and nt_order is not None:
                venue_order_id = nt_order.venue_order_id
            if venue_order_id is None:
                venue_order_id = self._cache.venue_order_id(client_order_id)
            if venue_order_id is None:
                continue
            bet = self._current_bets.get(str(venue_order_id))
            if bet is not None and not self._bet_is_cancelled(bet):
                continue
            if nt_order is None:
                continue
            self.generate_order_canceled(
                strategy_id=nt_order.strategy_id,
                instrument_id=nt_order.instrument_id,
                client_order_id=client_order_id,
                venue_order_id=VenueOrderId(str(venue_order_id)),
                ts_event=self._clock.timestamp_ns(),
            )

    async def generate_fill_reports(self, command) -> list:
        return []

    def _resolve_se_instrument(self, market_id: str, selection_id: str):
        if not market_id or not selection_id:
            return None
        for inst in self._cache.instruments(Venue(SHARPEXCH)):
            if (
                str(getattr(inst, "market_id", "")) == market_id
                and str(getattr(inst, "selection_id", "")) == selection_id
            ):
                return inst
        return None

    def _build_order_report(self, bet):
        from decimal import Decimal

        from nautilus_trader.core.uuid import UUID4
        from nautilus_trader.execution.reports import OrderStatusReport
        from nautilus_trader.model.enums import OrderSide
        from nautilus_trader.model.enums import OrderStatus
        from nautilus_trader.model.enums import OrderType
        from nautilus_trader.model.enums import TimeInForce
        from nautilus_trader.model.identifiers import VenueOrderId

        prog = bet_order_progress(bet)
        if prog is None or prog["status"] == "unknown":
            return None
        voi = VenueOrderId(prog["offer_id"])
        client_order_id = self._cache.client_order_id(voi)
        nt_order = self._cache.order(client_order_id) if client_order_id is not None else None

        if prog["side"] == "BACK":
            order_side = OrderSide.BUY
        elif prog["side"] == "LAY":
            order_side = OrderSide.SELL
        elif nt_order is not None:
            order_side = nt_order.side
        else:
            self._log.warning(f"SE reconcile: offerId={prog['offer_id']} has no side and no NT order; skip")
            return None

        if nt_order is not None:
            instrument_id = nt_order.instrument_id
            inst = self._cache.instrument(instrument_id)
        else:
            inst = self._resolve_se_instrument(prog["market_id"], prog["selection_id"])
            if inst is None:
                self._log.warning(
                    f"SE reconcile: offerId={prog['offer_id']} cannot resolve instrument"
                    f"(market={prog['market_id']},selection={prog['selection_id']}); skip",
                )
                return None
            instrument_id = inst.id
        if inst is None:
            return None

        status_map = {
            "accepted": OrderStatus.ACCEPTED,
            "partially_filled": OrderStatus.PARTIALLY_FILLED,
            "filled": OrderStatus.FILLED,
        }
        now = self._clock.timestamp_ns()
        quantity = nt_order.quantity if nt_order is not None else inst.make_qty(prog["original_qty"])
        if nt_order is not None:
            price = nt_order.price
        else:
            price = inst.make_price(prog["price"]) if prog["price"] > 0 else None
        return OrderStatusReport(
            account_id=self.account_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=voi,
            order_side=order_side,
            order_type=nt_order.order_type if nt_order is not None else OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            order_status=status_map[prog["status"]],
            quantity=quantity,
            filled_qty=inst.make_qty(prog["filled_qty"]),
            report_id=UUID4(),
            ts_accepted=nt_order.ts_init if nt_order is not None else now,
            ts_last=now,
            ts_init=now,
            price=price,
            avg_px=Decimal(str(prog["avg_px"])) if prog["avg_px"] > 0 else None,
        )

    async def generate_order_status_reports(self, command) -> list:
        if not await self._ensure_exec_snapshot_fresh() or self._last_current_bets_ns <= 0:
            if self._venue_liveness is not None:
                self._venue_liveness.mark_order_dead(SHARPEXCH)
            return []
        reports = []
        for bet in self._current_bets.values():
            report = self._build_order_report(bet)
            if report is not None:
                reports.append(report)
        if self._venue_liveness is not None:
            self._venue_liveness.mark_order_alive(SHARPEXCH)
        return reports

    async def generate_order_status_report(self, command):
        if not await self._ensure_exec_snapshot_fresh() or self._last_current_bets_ns <= 0:
            if self._venue_liveness is not None:
                self._venue_liveness.mark_order_dead(SHARPEXCH)
            return None
        target_voi = str(command.venue_order_id) if command.venue_order_id is not None else None
        target_coid = command.client_order_id
        for offer_id, bet in self._current_bets.items():
            if target_voi is not None and offer_id != target_voi:
                continue
            report = self._build_order_report(bet)
            if report is None:
                continue
            if target_coid is not None and report.client_order_id != target_coid:
                continue
            if self._venue_liveness is not None:
                self._venue_liveness.mark_order_alive(SHARPEXCH)
            return report
        if self._venue_liveness is not None:
            self._venue_liveness.mark_order_alive(SHARPEXCH)
        return None

    async def generate_position_status_reports(self, command) -> list:
        if not await self._ensure_exec_snapshot_fresh() or self._last_current_bets_ns <= 0:
            if self._venue_liveness is not None:
                self._venue_liveness.mark_position_dead(SHARPEXCH)
            return []

        reports = self._build_position_status_reports_from_current_bets()
        if self._venue_liveness is not None:
            self._venue_liveness.mark_position_alive(SHARPEXCH)
        return reports

    def _build_position_status_reports_from_current_bets(self) -> list:
        """从当前完整 CURRENT_BETS 快照构造仓位报告，不修改 liveness。"""
        from decimal import Decimal

        from nautilus_trader.core.uuid import UUID4
        from nautilus_trader.execution.reports import PositionStatusReport
        from nautilus_trader.model.enums import PositionSide

        reports = []
        now = self._clock.timestamp_ns()
        for pos in current_bets_to_positions(self._current_bets.values()):
            inst = self._resolve_se_instrument(pos["market_id"], pos["selection_id"])
            if inst is None:
                self._log.warning(
                    f"SE position reconcile: cannot resolve instrument(market={pos['market_id']},"
                    f"selection={pos['selection_id']}); skip",
                )
                continue
            reports.append(
                PositionStatusReport(
                    account_id=self.account_id,
                    instrument_id=inst.id,
                    position_side=PositionSide.LONG if pos["side"] == "LONG" else PositionSide.SHORT,
                    quantity=inst.make_qty(pos["qty"]),
                    report_id=UUID4(),
                    ts_last=now,
                    ts_init=now,
                    avg_px_open=Decimal(str(pos["avg_px"])) if pos["avg_px"] > 0 else None,
                ),
            )
        return reports


def se_balance_to_account_balances(balance: float, currency=USD) -> list:
    """SE `BALANCE` 帧 → AccountBalance(纯映射,可单测)。

    SE 入站边界统一换算为 USD 口径后,余额语义与 OE 一致:WS 余额已含挂单占用,
    因此 total = free = balance,locked = 0。
    """

    money = Money(balance, currency)
    return [AccountBalance(money, Money(0, currency), money)]


_BALANCE_FIELD_CANDIDATES = (
    "balance", "availableBalance", "accountBalance", "cashBalance",
    "currentBalance", "totalBalance", "walletBalance",
    "cash", "wallet", "balanceAmount", "balanceUsd", "availableToBet",
)


def _extract_balance_from_response(body: dict) -> float | None:
    """从 SE API 响应 dict 中提取余额值。

    先在顶层查找,再浅搜索嵌套子 dict 的第一层。
    """
    for key in _BALANCE_FIELD_CANDIDATES:
        val = body.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    # 浅搜索第一层嵌套 dict
    for v in body.values():
        if not isinstance(v, dict):
            continue
        for key in _BALANCE_FIELD_CANDIDATES:
            val = v.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
    return None


def _resolve_future(fut) -> None:
    if fut is not None and not fut.done():
        fut.set_result(None)


def se_order_to_place_bets_payload(
    order: SharpExchLegacyOrder,
    fx: float,
    *,
    timestamp_ms: int | None = None,
    persistence_type: str = "LAPSE",
    uuid_suffix: str | None = None,
) -> tuple[dict, str]:
    """SE legacy order → `/customer/api/placeBets` payload。

    SE 前端和 CURRENT_BETS 均为 USD 口径,因此出站 payload 直接使用 USD stake。
    函数不触发 Playwright/网络。
    """

    if fx <= 0:
        raise ValueError(f"Invalid fx: {fx}")
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    if uuid_suffix is None:
        alphabet = string.ascii_lowercase + string.digits
        uuid_suffix = "".join(random.choice(alphabet) for _ in range(5))

    odds_price = _clamp_odds_price(order.price)
    venue_size = round(order.size, 2)
    bet_uuid = (
        f"{order.market_id}_{order.selection_id}_{int(order.handicap)}__{timestamp_ms}-{uuid_suffix}"
    )
    bet_data = {
        "selectionId": int(order.selection_id),
        "handicap": _json_handicap(order.handicap),
        "price": odds_price,
        "size": venue_size,
        "side": order.side,
        "betUuid": bet_uuid,
        "betType": "EXCHANGE",
        "netPLBetslipEnabled": False,
        "netPLMarketPageEnabled": False,
        "quickStakesEnabled": True,
        "confirmBetsEnabled": False,
        "applicationType": "WEB",
        "mobile": False,
        "isEachWay": False,
        "eachWayData": {},
        "page": "competition",
        "persistenceType": persistence_type,
        "placedUsingEnterKey": False,
        "showLayOddsEnabled": False,
    }
    if order.order_type == "FOK":
        bet_data["fillOrKill"] = True
    return {order.market_id: [bet_data]}, bet_uuid


def parse_place_bets_response(response, market_id: str, bet_uuid: str) -> dict:
    """SE/OE 型 `placeBets` 响应 → 简单执行结果。

    只解析响应结构,不修改订单对象、不触发 NT event。
    """

    if not response:
        return {
            "success": False,
            "venue_order_id": None,
            "message": "No response",
            "transport_unknown": True,
        }
    if not isinstance(response, dict):
        return {
            "success": False,
            "venue_order_id": None,
            "message": "Invalid response",
            "transport_unknown": True,
        }
    if response.get("error"):
        result = {
            "success": False,
            "venue_order_id": None,
            "message": str(response.get("error")),
        }
        if response.get("_transport_error"):
            result["transport_unknown"] = True
        return result
    error_code = response.get("code")
    if error_code and error_code != 200:
        return {
            "success": False,
            "venue_order_id": None,
            "message": str(response.get("message") or f"code={error_code}"),
        }

    market_response = response.get(market_id)
    if isinstance(market_response, dict) and market_response.get("status") == "OK":
        offer_ids = market_response.get("offerIds", {}) or {}
        venue_order_id = offer_ids.get(bet_uuid)
        if venue_order_id is None and offer_ids:
            venue_order_id = next(iter(offer_ids.values()))
        return {
            "success": True,
            "venue_order_id": str(venue_order_id or bet_uuid),
            "message": "Order placed successfully",
        }

    if isinstance(market_response, dict):
        message = market_response.get("message") or market_response.get("status") or "Unknown market error"
    else:
        message = f"No response for market {market_id}"
    result = {
        "success": False,
        "venue_order_id": None,
        "message": str(message),
    }
    if market_response is None:
        result["transport_unknown"] = True
    return result


def se_order_to_cancel_bets_payload(
    market_id: str,
    venue_order_id: str,
    *,
    bet: dict | None = None,
) -> dict | None:
    """SE/OE 型 cancel request body。

    缺 market 或 offer id 时返回 None,由后续 ExecutionClient 生成 cancel rejected。
    """

    market_id = str(market_id or "")
    venue_order_id = str(venue_order_id or "")
    if not market_id or not venue_order_id:
        return None
    if isinstance(bet, dict) and bet:
        payload_bet = dict(bet)
        payload_bet["marketId"] = str(payload_bet.get("marketId") or market_id)
        payload_bet["offerId"] = payload_bet.get("offerId") or _coerce_offer_id(venue_order_id)
        payload_bet["betType"] = payload_bet.get("betType") or "EXCHANGE"
        return {market_id: [payload_bet]}
    return {
        market_id: [
            {
                "offerId": venue_order_id,
                "betType": "EXCHANGE",
            },
        ],
    }


def _coerce_offer_id(value: str):
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def _json_handicap(value: float):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0
    if numeric.is_integer():
        return int(numeric)
    return numeric


def parse_cancel_bets_response(response) -> dict:
    """SE/OE 型 `cancelBets` 响应 → 简单撤单结果。"""

    if not response:
        return {"success": False, "message": "Cancel failed", "transport_unknown": True}
    if not isinstance(response, dict):
        return {"success": False, "message": "Invalid response", "transport_unknown": True}
    if response.get("error"):
        result = {
            "success": False,
            "message": str(response.get("error")),
        }
        if response.get("_transport_error"):
            result["transport_unknown"] = True
        return result
    return {"success": True, "message": "Order cancelled via API"}


def nt_order_to_legacy_order(nt_order, inst) -> SharpExchLegacyOrder | None:
    """NT `Order` + SE `BettingInstrument` → SE legacy order。

    - `market_id` / `selection_id` / `handicap` 取自 instrument;
    - NT `BUY` → SE `BACK`,NT `SELL` → SE `LAY`;
    - 只做字段翻译,SE 当前按 USD 原生 stake 出站,不做 FX 换算。
    """

    from nautilus_trader.model.enums import OrderSide as _NTOrderSide

    market_id = str(getattr(inst, "market_id", "") or "")
    selection_id = str(getattr(inst, "selection_id", "") or "")
    if not market_id or not selection_id:
        return None
    side = "BACK" if nt_order.side == _NTOrderSide.BUY else "LAY"
    return SharpExchLegacyOrder(
        venue="sharpexch",
        market_id=market_id,
        selection_id=selection_id,
        handicap=_coerce_handicap(getattr(inst, "selection_handicap", None)),
        side=side,
        price=float(nt_order.price),
        size=float(nt_order.quantity),
        order_type="GTC",
    )


def normalize_current_bets_to_usd(bets, fx: float) -> list:
    """SE CURRENT_BETS 的金额/size 字段从 venue 币种归一到 USD。

    价格字段(`price`/`averagePrice`)不做换算。
    """

    if fx <= 0:
        fx = 1.0
    return [_normalize_bet_amounts_to_usd(bet, fx) for bet in (bets or [])]


def current_bets_to_fills(bets) -> list:
    """SE `CURRENT_BETS` 快照 → 累计成交列表。

    `sizeMatched` 本身是累计成交量;只有 `sizeMatched > 0` 且 `averagePrice > 0` 时才产生成交。
    """

    fills = []
    for bet in bets or []:
        offer_id = str(bet.get("offerId", "") or "")
        if not offer_id:
            continue
        size_matched = float(bet.get("sizeMatched", 0) or 0)
        avg_price = float(bet.get("averagePrice", 0) or 0)
        if size_matched > 0 and avg_price > 0:
            fills.append(
                {
                    "offer_id": offer_id,
                    "avg_price": avg_price,
                    "size_matched": size_matched,
                },
            )
    return fills


def bet_order_progress(bet) -> dict | None:
    """SE `CURRENT_BETS` 单 bet → 订单进度。

    返回 dict 供后续 reconcile report 使用;缺 `offerId` 时返回 None。
    """

    offer_id = str(bet.get("offerId", "") or "")
    if not offer_id:
        return None
    size_matched = float(bet.get("sizeMatched", 0) or 0)
    size_remaining = float(bet.get("sizeRemaining", 0) or 0)
    size_placed = bet.get("sizePlaced")
    original_qty = (
        float(size_placed) if size_placed not in (None, "")
        else size_matched + size_remaining
    )
    if size_remaining > 0 and size_matched > 0:
        status = "partially_filled"
    elif size_remaining > 0:
        status = "accepted"
    elif size_matched > 0:
        status = "filled"
    else:
        status = "unknown"
    return {
        "offer_id": offer_id,
        "market_id": str(bet.get("marketId", "") or ""),
        "selection_id": str(bet.get("selectionId", "") or ""),
        "side": str(bet.get("side", "") or "").upper(),
        "original_qty": original_qty,
        "filled_qty": size_matched,
        "avg_px": float(bet.get("averagePrice", 0) or 0),
        "price": float(bet.get("price", 0) or 0),
        "status": status,
    }


def current_bets_to_positions(bets) -> list:
    """SE `CURRENT_BETS` 快照 → 每 selection 净持仓。

    BACK=LONG / LAY=SHORT;反向 matched 只抵减净额,不进入主方向均价。
    """

    acc: dict = {}
    for bet in bets or []:
        progress = bet_order_progress(bet)
        if progress is None:
            continue
        matched = float(progress["filled_qty"])
        if matched <= 0:
            continue
        avg = float(progress["avg_px"])
        bucket = acc.setdefault(
            (progress["market_id"], progress["selection_id"]),
            {"long_q": 0.0, "long_w": 0.0, "short_q": 0.0, "short_w": 0.0},
        )
        if progress["side"] == "BACK":
            bucket["long_q"] += matched
            bucket["long_w"] += matched * avg
        elif progress["side"] == "LAY":
            bucket["short_q"] += matched
            bucket["short_w"] += matched * avg

    positions = []
    for (market_id, selection_id), bucket in acc.items():
        net = bucket["long_q"] - bucket["short_q"]
        if abs(net) < 1e-9:
            continue
        if net > 0:
            side = "LONG"
            avg_px = bucket["long_w"] / bucket["long_q"] if bucket["long_q"] > 0 else 0.0
        else:
            side = "SHORT"
            avg_px = bucket["short_w"] / bucket["short_q"] if bucket["short_q"] > 0 else 0.0
        positions.append(
            {
                "market_id": market_id,
                "selection_id": selection_id,
                "side": side,
                "qty": abs(net),
                "avg_px": avg_px,
            },
        )
    return positions


def _normalize_bet_amounts_to_usd(bet: dict, fx: float) -> dict:
    normalized = dict(bet)
    for key, value in bet.items():
        if not _is_fx_amount_key(key):
            continue
        amount = _try_float(value)
        if amount is None:
            continue
        normalized[key] = amount * fx
    return normalized


def _is_fx_amount_key(key: str) -> bool:
    key_lower = str(key).lower()
    return key_lower.startswith("size") or key_lower in _FX_AMOUNT_KEYS


def _try_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_handicap(value) -> float:
    """NT 无 handicap sentinel / NaN / None → 0.0。"""

    from nautilus_trader.model.instruments.betting import null_handicap

    try:
        handicap = float(value)
    except (TypeError, ValueError):
        return 0.0
    if handicap != handicap or handicap == float(null_handicap()):
        return 0.0
    return handicap


def _clamp_odds_price(price: float) -> float:
    odds_price = round(price, 2) if price > 0 else 1.01
    if odds_price < 1.01:
        return 1.01
    if odds_price > 1000:
        return 1000
    return odds_price
