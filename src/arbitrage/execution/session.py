"""
ArbExecutionSessionMixin —— PM 子类 / OE 客户端共用的执行 session 核心。

详细设计:`docs/arbitrage/architectures/execution/architecture.md §4.1 / §4.2 / §4.4 / §3.4`。

职责(execution = 执行 + 追踪,不做决策,Q13):
- **session 入口**(`_begin_session`,由各 client `_submit_order` 调):残留挂单检测 →
  cancel-only(撤残留 + 丢弃当次 submit,reject 让 strategy 下轮重发)vs submit+track。
- **tracking 超时**(§4.2):NT clock 绝对超时,terminal 抢先取消;超时即结束 session 不补救。
- **同步**(§3.4 / §6.10):`_execution_active` = 在飞 session 数 > 0,供 strategy 全局 `is_execution_active`
  callable 直读(Q19 ≤1 执行)。**#108**:`execution.*` 消息已退役(健康⊥执行互斥删除,无消费者)。

宿主契约:`class ArbXxxClient(ArbExecutionSessionMixin, <NT ExecutionClient>)`(mixin 在前,
覆盖 `_send_order_event`);`__init__` 末尾调 `self._init_arb_session(...)`;
依赖 NT 基类提供 `self._clock / self._msgbus / self._cache / self._log / generate_order_rejected`。
撤残留挂单是 venue IO → 子类覆盖 `_cancel_residual_orders`。
"""

from __future__ import annotations

from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.model.events import OrderAccepted
from nautilus_trader.model.events import OrderCanceled
from nautilus_trader.model.events import OrderCancelRejected
from nautilus_trader.model.events import OrderExpired
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.events import OrderRejected
from nautilus_trader.model.objects import AccountBalance
from nautilus_trader.model.objects import Money

from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.common.venues import descriptor_for
from src.arbitrage.common.venues import order_liability
from src.arbitrage.common.venues import venue_id_from_instrument_id

# submit 终态(OrderFilled 仅在全成时,见 _send_order_event);cancel 终态只看撤单完成/失败。
_SUBMIT_TERMINAL = (OrderCanceled, OrderRejected, OrderExpired)
_CANCEL_TERMINAL = (OrderCanceled, OrderCancelRejected, OrderRejected, OrderExpired)

_TIMEOUT_PREFIX = "arb_exec_timeout:"


class ArbExecutionSessionMixin:

    # ── 宿主在 __init__ 末尾调用 ───────────────────────────────────────
    def _init_arb_session(
        self,
        *,
        session_timeout_secs: float,
        pair_registry: PairRegistry | None = None,
        pair_inflight=None,  # PairInFlightGate(§6.10 §7,per-pair 串行);None → 不参与
    ) -> None:
        self._pair_registry = pair_registry  # #34: matching 写,本类读;`_pair_id_for` 用
        self._pair_inflight = pair_inflight   # §6.10 §7:execution 段维护 per-pair session 计数,归 0 释放闸
        self._session_timeout_ns = secs_to_nanos(session_timeout_secs)
        # coid -> {kind, qty, price, filled, instrument_id, pair_id, venue_order_id}
        self._active_sessions: dict = {}

    @property
    def _execution_active(self) -> bool:
        """在飞 submit+track session 数 > 0(健康检查据此整 tick 让路,Q19/§6.10)。"""
        return len(self._active_sessions) > 0

    # ── session 入口(client._submit_order 内调)────────────────────────
    def _begin_session(self, command) -> bool:
        """True = 继续 submit+track;False = cancel-only,调用方应直接 return(本次 submit 丢弃)。"""
        order = command.order
        instrument_id = order.instrument_id

        residual = self._cache.orders_open(instrument_id=instrument_id)
        if residual:
            # cancel-only:撤残留挂单 + 丢弃当次 submit(reject 让 NT 解析,strategy 下轮全量重算重发)
            self._log.info(
                "Execution session cancel-only: residual open orders present "
                f"instrument_id={instrument_id}, "
                f"new_client_order_id={order.client_order_id}, "
                f"residuals={self._format_residual_orders(residual)}",
            )
            self._cancel_residual_orders(instrument_id, list(residual))
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=instrument_id,
                client_order_id=order.client_order_id,
                reason="cancel-only session: residual open orders present",
                ts_event=self._clock.timestamp_ns(),
            )
            return False

        return self._begin_order_session(order, kind="submit")

    def _begin_cancel_session(self, order) -> bool:
        """True = 已建立 cancel session;False = 同 order 已有 session,调用方不再重复撤。"""
        return self._begin_order_session(order, kind="cancel")

    def _begin_order_session(self, order, *, kind: str) -> bool:
        coid = order.client_order_id
        if coid in self._active_sessions:
            self._log.info(
                "Execution session already active: "
                f"kind={self._active_sessions[coid].get('kind')}, "
                f"client_order_id={coid}; skip duplicate {kind}",
            )
            return False

        instrument_id = order.instrument_id
        pair_id = self._pair_id_for(instrument_id)
        # #105 ②:watchdog 必须在 `exec_started` **之前**且与之原子 —— 保证"只要 exec_count++ 了,
        # 就一定有人(终态或看门狗)来减"。顺序:(1) 先 arm watchdog(本块唯一可能抛的操作,若抛则
        # 尚未改任何共享态,干净失败);(2) 再做纯 dict 置位(exec_started / active_sessions / arm,不会抛);
        # (3) publish 收尾(非关键:即便抛,watchdog + session 已就位,终会 _end_session)。
        self._clock.set_time_alert_ns(
            name=f"{_TIMEOUT_PREFIX}{coid.value}",
            alert_time_ns=self._clock.timestamp_ns() + self._session_timeout_ns,
            callback=self._on_session_timeout,
        )
        self._active_sessions[coid] = {
            "kind": kind,
            "qty": order.quantity.as_double(),
            "price": float(order.price) if order.has_price else None,
            "side": order.side,
            "filled": 0.0,
            "instrument_id": instrument_id,
            "pair_id": pair_id,
            "venue_order_id": getattr(order, "venue_order_id", None),
        }
        if pair_id is not None:
            # §6.10 §7:per-pair session 计数 ++(套利已由 strategy fire,所有权进入执行)
            if self._pair_inflight is not None:
                self._pair_inflight.exec_started(pair_id)
        # #108:不再 publish `execution.started`(OE DataClient 的健康⊥执行互斥已退役,无消费者)。
        return True

    # ── 唯一漏斗:所有 order event 经此(覆盖 NT cpdef)────────────────
    def _send_order_event(self, event) -> None:
        super()._send_order_event(event)  # 先正常上送 ExecEngine(NT 标准管道)

        sess = self._active_sessions.get(event.client_order_id)
        if sess is None:
            return
        kind = sess.get("kind", "submit")
        if kind == "submit" and isinstance(event, OrderAccepted):
            self._reserve_available_balance_for_accepted_order(event, sess)
            self._log.info(
                "Execution session accepted: "
                f"client_order_id={event.client_order_id}, "
                f"venue_order_id={event.venue_order_id}, "
                f"instrument_id={event.instrument_id}; tracking continues until terminal/timeout",
            )
        if kind == "cancel":
            terminal = isinstance(event, _CANCEL_TERMINAL)
        else:
            terminal = isinstance(event, _SUBMIT_TERMINAL)
        if kind == "submit" and isinstance(event, OrderFilled):
            sess["filled"] += event.last_qty.as_double()
            if sess["filled"] >= sess["qty"]:
                terminal = True  # 全成才终态;partial 不重置/不结束(绝对超时,§4.2)
        if terminal:
            self._end_session(event.client_order_id)

    # ── 超时(NT clock 绝对超时;超时即结束,不补救)──────────────────
    def _on_session_timeout(self, event) -> None:
        coid_str = event.name[len(_TIMEOUT_PREFIX):]
        for coid in list(self._active_sessions):
            if coid.value == coid_str:
                self._log.warning(f"Execution session timeout: {coid_str}")
                self._end_session(coid, timed_out=True)
                return

    def _end_session(self, coid, timed_out: bool = False) -> None:
        sess = self._active_sessions.pop(coid, None)
        if sess is None:
            return
        # #105 ②(出口对称):先清 per-pair 计数(保证 exec_count 一定回落),再做可能抛的
        # cancel_timer —— 否则它抛会漏减、in-flight 永久泄漏。
        # §6.10 §7:per-pair session 计数 --;归 0 → 释放 per-pair 闸(本笔套利执行结束)
        if self._pair_inflight is not None and sess["pair_id"] is not None:
            self._pair_inflight.exec_finished(sess["pair_id"])
        if not timed_out:
            self._clock.cancel_timer(f"{_TIMEOUT_PREFIX}{coid.value}")  # terminal 抢先取消 watchdog
        # #108:不再 publish `execution.finished`(健康⊥执行互斥已退役,无消费者)。

    # ── helpers / hooks ───────────────────────────────────────────────
    def _pair_id_for(self, instrument_id):
        # #34:pair_id 来自 matching 的 PairRegistry,**不是** info["competition"](后者是联赛名)
        if self._pair_registry is None:
            return None
        return self._pair_registry.get(instrument_id)

    def _reserve_available_balance_for_accepted_order(self, event: OrderAccepted, sess: dict) -> None:
        """accepted 后本地预扣可用余额。

        adapter 外统一 USD/系统基准币口径:OE 已在入站乘 fx、出站 place 前再除 fx;这里不做 fx 分支。
        """
        order = self._cache.order(event.client_order_id)
        if order is not None and order.has_price:
            instrument_id = order.instrument_id
            quantity = order.leaves_qty.as_double()
            price = float(order.price)
            side = order.side
        else:
            instrument_id = sess.get("instrument_id")
            quantity = float(sess.get("qty") or 0.0)
            price = sess.get("price")
            side = sess.get("side")
        if instrument_id is None or price is None or quantity <= 0:
            return

        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            return

        account = self._cache.account_for_venue(instrument_id.venue)
        if account is None:
            return

        currency = instrument.quote_currency
        free = account.balance_free(currency)
        if free is None:
            return

        reserved = accepted_order_reserved_notional(instrument_id, quantity, price, side=side)
        available = max(0.0, free.as_double() - reserved)
        balance = AccountBalance(
            total=Money(available, free.currency),
            locked=Money(0, free.currency),
            free=Money(available, free.currency),
        )
        self.generate_account_state(
            balances=[balance],
            margins=[],
            reported=True,
            ts_event=self._clock.timestamp_ns(),
        )

    def _cancel_residual_orders(self, instrument_id, residual: list) -> None:
        """#105:撤残单 —— **每条残单的撤单都是一次 tracked execution**,纳入 per-pair `exec_count`
        (`exec_count→0` 才清 in-flight)。这样 cancel-only 这次执行(撤单)与 submit+track 一样有头有尾,
        **撤单请求发出但 venue 终态未到时 in-flight 不会被提前清**;一条 session 都没起的 cancel-only
        也由 watchdog 兜底(无需 max-hold 等兜底)。子类实现 `_cancel_residual_one(order)`
        (真实 venue 撤单请求,async),最终由 `OrderCanceled`/`OrderCancelRejected` 或 timeout 收口。"""
        for order in residual:
            if self._begin_cancel_session(order):
                self._loop.create_task(self._tracked_residual_cancel(order))

    async def _tracked_residual_cancel(self, order) -> None:
        """#105:撤一条残单;session 由后续 NT cancel terminal 或 watchdog 收尾。"""
        try:
            await self._cancel_residual_one(order)
        except Exception as e:  # noqa: BLE001 — 撤单 IO 异常必须生成 NT cancel reject 来释放 session
            self._log.error(
                f"cancel-only: residual cancel failed for {getattr(order, 'client_order_id', '')}: {e!r}",
            )
            self.generate_order_cancel_rejected(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=getattr(order, "venue_order_id", None),
                reason=f"residual cancel exception: {e!r}",
                ts_event=self._clock.timestamp_ns(),
            )

    async def _cancel_residual_one(self, order) -> None:
        """子类实现真实 venue 撤单(OE Playwright / PM CLOB)。base 占位告警。"""
        self._log.warning(
            f"cancel-only: no venue cancel impl for {getattr(order, 'client_order_id', '')}; "
            "override _cancel_residual_one",
        )

    @staticmethod
    def _format_residual_orders(residual: list) -> list[dict]:
        return [
            {
                "client_order_id": str(getattr(order, "client_order_id", "")),
                "venue_order_id": str(getattr(order, "venue_order_id", "")),
            }
            for order in residual
        ]

    def _active_cancel_sessions_snapshot(self) -> list[tuple[object, dict]]:
        """给 adapter 把 venue 真值转换成 NT cancel terminal 用;返回浅拷贝避免迭代中变更。"""
        return [
            (coid, dict(sess))
            for coid, sess in self._active_sessions.items()
            if sess.get("kind") == "cancel"
        ]


def accepted_order_reserved_notional(instrument_id, quantity: float, price: float, *, side="BUY") -> float:
    """OrderAccepted 后要从可用余额里保守预扣的金额。

    公式只由 Venue Registry capability 决定:
    - probability venue: quantity 是 share,成本 = share * probability
    - decimal BACK:quantity 已是系统基准币 stake
    - decimal LAY:liability = quantity * (odds - 1)
    """
    venue = venue_id_from_instrument_id(instrument_id)
    side_name = str(getattr(side, "name", side) or "").rsplit(".", 1)[-1].upper()
    descriptor = descriptor_for(venue)
    if descriptor.odds_model == "probability" and side_name == "SELL":
        return 0.0
    is_lay = descriptor.odds_model == "decimal" and side_name == "SELL"
    return order_liability(venue, quantity, price, is_lay=is_lay)
