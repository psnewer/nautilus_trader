"""
ArbExecutionSessionMixin —— PM 子类 / OE 客户端共用的执行 session 核心。

详细设计:`docs/arbitrage/architectures/execution/architecture.md §4.1 / §4.2 / §4.4 / §3.4`。

职责(execution = 执行 + 追踪,不做决策,Q13):
- **session 入口**(`_begin_session`,由各 client `_submit_order` 调):残留挂单检测 →
  cancel-only(撤残留 + 丢弃当次 submit,reject 让 strategy 下轮重发)vs submit+track。
- **leg_settled**(§4.4):覆盖唯一漏斗 `_send_order_event` —— 任一 venue 确认事件 →
  `mark(pair_id, instrument_id)`(腿键=instrument_id,#25)。submit+track 启动时 `arm`。
- **tracking 超时**(§4.2):NT clock 绝对超时,terminal 抢先取消;超时即结束 session 不补救。
- **同步**(§3.4 / §6.10):submit+track 启动 publish `execution.started`、terminal/timeout
  publish `execution.finished`;`_execution_active` = 在飞 session 数 > 0(ref-count,健康检查据此让路)。

宿主契约:`class ArbXxxClient(ArbExecutionSessionMixin, <NT ExecutionClient>)`(mixin 在前,
覆盖 `_send_order_event`);`__init__` 末尾调 `self._init_arb_session(...)`;
依赖 NT 基类提供 `self._clock / self._msgbus / self._cache / self._log / generate_order_rejected`。
撤残留挂单是 venue IO → 子类覆盖 `_cancel_residual_orders`。
"""

from __future__ import annotations

from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.model.events import OrderAccepted
from nautilus_trader.model.events import OrderCanceled
from nautilus_trader.model.events import OrderExpired
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.events import OrderRejected

from src.arbitrage.common.leg_settled import LegSettledRegistry
from src.arbitrage.common.pair_registry import PairRegistry

# venue 确认事件(任一落地 → leg_settled=true,§4.4);OrderSubmitted/Denied 非 venue 确认,不计
_VENUE_CONFIRM = (OrderAccepted, OrderFilled, OrderCanceled, OrderRejected, OrderExpired)
# 终态(结束 session;OrderFilled 仅在全成时,见 _send_order_event)
_TERMINAL = (OrderCanceled, OrderRejected, OrderExpired)

_TIMEOUT_PREFIX = "arb_exec_timeout:"


class ArbExecutionSessionMixin:

    # ── 宿主在 __init__ 末尾调用 ───────────────────────────────────────
    def _init_arb_session(
        self,
        *,
        leg_settled: LegSettledRegistry,
        session_timeout_secs: float,
        pair_registry: PairRegistry | None = None,
        pair_inflight=None,  # PairInFlightGate(§6.10 §7,per-pair 串行);None → 不参与
    ) -> None:
        self._leg_settled = leg_settled
        self._pair_registry = pair_registry  # #34: matching 写,本类读;`_pair_id_for` 用
        self._pair_inflight = pair_inflight   # §6.10 §7:execution 段维护 per-pair session 计数,归 0 释放闸
        self._session_timeout_ns = secs_to_nanos(session_timeout_secs)
        # coid -> {qty, filled, instrument_id, pair_id}
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
            self._cancel_residual_orders(instrument_id, list(residual))
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=instrument_id,
                client_order_id=order.client_order_id,
                reason="cancel-only session: residual open orders present",
                ts_event=self._clock.timestamp_ns(),
            )
            return False

        coid = order.client_order_id
        pair_id = self._pair_id_for(instrument_id)
        if pair_id is not None:
            self._leg_settled.arm(pair_id, instrument_id)
            # §6.10 §7:per-pair session 计数 ++(套利已由 strategy fire,所有权进入执行)
            if self._pair_inflight is not None:
                self._pair_inflight.exec_started(pair_id)
        self._active_sessions[coid] = {
            "qty": order.quantity.as_double(),
            "filled": 0.0,
            "instrument_id": instrument_id,
            "pair_id": pair_id,
        }
        self._publish_execution("execution.started", instrument_id, pair_id)
        self._clock.set_time_alert_ns(
            name=f"{_TIMEOUT_PREFIX}{coid.value}",
            alert_time_ns=self._clock.timestamp_ns() + self._session_timeout_ns,
            callback=self._on_session_timeout,
        )
        return True

    # ── 唯一漏斗:所有 order event 经此(覆盖 NT cpdef)────────────────
    def _send_order_event(self, event) -> None:
        super()._send_order_event(event)  # 先正常上送 ExecEngine(NT 标准管道)

        if isinstance(event, _VENUE_CONFIRM):
            pair_id = self._pair_id_for(event.instrument_id)
            if pair_id is not None:
                self._leg_settled.mark(pair_id, event.instrument_id)

        sess = self._active_sessions.get(event.client_order_id)
        if sess is None:
            return
        terminal = isinstance(event, _TERMINAL)
        if isinstance(event, OrderFilled):
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
        if not timed_out:
            self._clock.cancel_timer(f"{_TIMEOUT_PREFIX}{coid.value}")  # terminal 抢先取消 watchdog
        self._publish_execution("execution.finished", sess["instrument_id"], sess["pair_id"])
        # §6.10 §7:per-pair session 计数 --;归 0 → 释放 per-pair 闸(本笔套利执行结束)
        if self._pair_inflight is not None and sess["pair_id"] is not None:
            self._pair_inflight.exec_finished(sess["pair_id"])

    # ── helpers / hooks ───────────────────────────────────────────────
    def _pair_id_for(self, instrument_id):
        # #34:pair_id 来自 matching 的 PairRegistry,**不是** info["competition"](后者是联赛名)
        if self._pair_registry is None:
            return None
        return self._pair_registry.get(instrument_id)

    def _publish_execution(self, topic: str, instrument_id, pair_id) -> None:
        self._msgbus.publish(topic=topic, msg={"instrument_id": instrument_id, "pair_id": pair_id})

    def _cancel_residual_orders(self, instrument_id, residual: list) -> None:
        """撤残留挂单(venue IO)。基类仅告警避免无声;PM/OE 子类覆盖做真实撤单。"""
        self._log.warning(
            f"cancel-only: {len(residual)} residual order(s) on {instrument_id}; "
            "override _cancel_residual_orders for venue cancel",
        )
