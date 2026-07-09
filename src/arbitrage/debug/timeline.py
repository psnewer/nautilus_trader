"""
订单状态时序模拟(Q11.4 timeline.py)。

设计见 `architectures/_cross-cutting/debug-injection.md` 和 `debug-framework.md`。

用途:在 Live 测试中模拟真实订单状态转换时序,如:
- 部分成交(partial fill)
- 拒单(rejection)
- 撤单(cancellation)
- 延迟成交

**NT-pure 实现**:使用 `Clock.set_time_alert_ns()` 调度状态变化,不用 `asyncio.sleep`。

配置格式(mock_data.timeline):
```json
{
  "category": "timeline",
  "enabled": true,
  "conditions": {"venue": "sharpexch"},
  "data": {
    "steps": [
      {"event": "ACCEPT", "delay_ms": 100},
      {"event": "PARTIAL_FILL", "delay_ms": 500, "fill_pct": 0.5},
      {"event": "FILL", "delay_ms": 1000}
    ]
  }
}
```

事件类型:
- ACCEPT: 订单接受(generate_order_accepted)
- PARTIAL_FILL: 部分成交,fill_pct=0.5 表示填 50%
- FILL: 全部成交(剩余部分)
- REJECT: 拒单(generate_order_rejected)
- CANCEL: 撤单(generate_order_canceled)
- EXPIRE: 过期(generate_order_expired)

消费方:SkipExecution* 客户端在 `_submit_order` 时查询 timeline 配置,
有则按 timeline 执行状态转换,否则走默认"立即全成"。
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity

from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.debug.config import MockCategory

if TYPE_CHECKING:
    from nautilus_trader.common.component import Clock
    from nautilus_trader.core.message import Event
    from nautilus_trader.execution.client import ExecutionClient


class TimelineEvent(Enum):
    """订单状态时序事件类型。"""

    ACCEPT = "ACCEPT"              # 订单被接受
    PARTIAL_FILL = "PARTIAL_FILL"  # 部分成交
    FILL = "FILL"                  # 全部成交(或剩余部分)
    REJECT = "REJECT"              # 拒单
    CANCEL = "CANCEL"              # 撤单
    EXPIRE = "EXPIRE"              # 过期


@dataclass
class TimelineStep:
    """单个时序步骤。"""

    event: TimelineEvent
    delay_ms: int = 0           # 事件触发前延迟(毫秒)
    fill_pct: float = 1.0       # PARTIAL_FILL 时填充百分比(0-1)
    reject_reason: str = ""     # REJECT 时拒单原因
    cancel_reason: str = ""     # CANCEL 时撤单原因

    @classmethod
    def from_dict(cls, raw: dict) -> "TimelineStep":
        return cls(
            event=TimelineEvent(raw.get("event", "FILL")),
            delay_ms=int(raw.get("delay_ms", 0)),
            fill_pct=float(raw.get("fill_pct", 1.0)),
            reject_reason=str(raw.get("reject_reason", "")),
            cancel_reason=str(raw.get("cancel_reason", "")),
        )


@dataclass
class TimelineConfig:
    """订单时序配置。"""

    steps: list[TimelineStep]

    @classmethod
    def from_dict(cls, raw: dict) -> "TimelineConfig":
        steps_raw = raw.get("steps") or []
        return cls(steps=[TimelineStep.from_dict(s) for s in steps_raw])


def get_timeline_config(debug: DebugConfig, context: dict | None = None) -> TimelineConfig | None:
    """从 DebugConfig 获取 timeline 配置。"""
    mock = debug.get_mock(MockCategory.TIMELINE, context)
    if mock is None or not isinstance(mock.data, dict):
        return None
    return TimelineConfig.from_dict(mock.data)


def _timeline_context(order) -> dict:
    """构造 timeline mock_data conditions 的匹配上下文。"""
    instrument_id = str(order.instrument_id)
    venue = instrument_id.rsplit(".", 1)[-1].lower()
    side = getattr(order, "side", None)
    order_type = getattr(order, "order_type", None)
    return {
        "instrument_id": instrument_id,
        "venue": venue,
        "venue_upper": venue.upper(),
        "order_side": str(getattr(side, "name", side)) if side else None,
        "order_type": str(getattr(order_type, "name", order_type)) if order_type else None,
    }


@dataclass
class _TimelineState:
    """Timeline 执行状态(跨 callback 共享)。"""

    order: object
    venue_order_id: VenueOrderId
    total_qty: Decimal
    filled_qty: Decimal = field(default_factory=lambda: Decimal("0"))
    step_index: int = 0
    terminated: bool = False


class TimelineExecutor:
    """执行订单状态时序模拟。

    **NT-pure 实现**:使用 `Clock.set_time_alert_ns()` 调度状态变化。

    使用方式:
    ```python
    executor = TimelineExecutor(client, debug_config, quote_currency)
    if executor.has_timeline(order):
        executor.execute(command)  # 同步调用,内部用 clock 调度
    else:
        # 走默认立即全成
        _mock_fill(client, command, quote_currency)
    ```
    """

    def __init__(
        self,
        client: "ExecutionClient",
        debug: DebugConfig,
        quote_currency,
    ) -> None:
        self._client = client
        self._clock: "Clock" = client._clock
        self._debug = debug
        self._quote_currency = quote_currency
        self._states: dict[str, _TimelineState] = {}  # client_order_id -> state

    def has_timeline(self, order) -> bool:
        """检查该订单是否有 timeline 配置。"""
        context = _timeline_context(order)
        return get_timeline_config(self._debug, context) is not None

    def execute(self, command) -> None:
        """按 timeline 配置执行订单状态转换(同步,用 Clock 调度)。"""
        order = command.order
        context = _timeline_context(order)
        timeline = get_timeline_config(self._debug, context)

        if timeline is None or not timeline.steps:
            # 无 timeline 配置,走立即全成
            self._immediate_fill(command)
            return

        # 先调 _begin_session 保留 session/gate 生命周期
        if not self._client._begin_session(command):
            return

        # 初始化状态
        venue_order_id = VenueOrderId(f"MOCK-{order.client_order_id.value}")
        state = _TimelineState(
            order=order,
            venue_order_id=venue_order_id,
            total_qty=Decimal(str(order.quantity)),
        )
        cid = order.client_order_id.value
        self._states[cid] = state

        # 调度第一个 step
        self._schedule_step(cid, timeline, 0)

    def _schedule_step(self, cid: str, timeline: TimelineConfig, step_index: int) -> None:
        """调度指定 step 的执行。"""
        if step_index >= len(timeline.steps):
            # 所有 step 执行完毕,清理状态
            self._states.pop(cid, None)
            return

        step = timeline.steps[step_index]
        state = self._states.get(cid)
        if state is None or state.terminated:
            return

        state.step_index = step_index

        if step.delay_ms <= 0:
            # 无延迟,立即执行
            self._execute_step(cid, timeline)
        else:
            # 用 Clock.set_time_alert_ns 调度
            alert_time_ns = self._clock.timestamp_ns() + step.delay_ms * 1_000_000
            timer_name = f"timeline-{cid}-{step_index}"

            def callback(event: "Event") -> None:
                self._execute_step(cid, timeline)

            self._clock.set_time_alert_ns(
                name=timer_name,
                alert_time_ns=alert_time_ns,
                callback=callback,
            )

    def _execute_step(self, cid: str, timeline: TimelineConfig) -> None:
        """执行当前 step 并调度下一个。"""
        state = self._states.get(cid)
        if state is None or state.terminated:
            return

        step = timeline.steps[state.step_index]
        order = state.order
        venue_order_id = state.venue_order_id

        if step.event == TimelineEvent.ACCEPT:
            self._generate_accepted(order, venue_order_id)

        elif step.event == TimelineEvent.PARTIAL_FILL:
            fill_qty = state.total_qty * Decimal(str(step.fill_pct))
            if fill_qty > 0 and state.filled_qty < state.total_qty:
                actual_fill = min(fill_qty, state.total_qty - state.filled_qty)
                self._generate_fill(order, venue_order_id, actual_fill)
                state.filled_qty += actual_fill

        elif step.event == TimelineEvent.FILL:
            # 填充剩余部分
            remaining = state.total_qty - state.filled_qty
            if remaining > 0:
                self._generate_fill(order, venue_order_id, remaining)
                state.filled_qty = state.total_qty

        elif step.event == TimelineEvent.REJECT:
            self._generate_rejected(order, venue_order_id, step.reject_reason)
            state.terminated = True
            self._states.pop(cid, None)
            return  # 拒单后终止

        elif step.event == TimelineEvent.CANCEL:
            self._generate_canceled(order, venue_order_id, step.cancel_reason)
            state.terminated = True
            self._states.pop(cid, None)
            return  # 撤单后终止

        elif step.event == TimelineEvent.EXPIRE:
            self._generate_expired(order, venue_order_id)
            state.terminated = True
            self._states.pop(cid, None)
            return  # 过期后终止

        # 调度下一个 step
        self._schedule_step(cid, timeline, state.step_index + 1)

    def _immediate_fill(self, command) -> None:
        """默认立即全成。"""
        if not self._client._begin_session(command):
            return
        order = command.order
        venue_order_id = VenueOrderId(f"MOCK-{order.client_order_id.value}")
        self._generate_accepted(order, venue_order_id)
        self._generate_fill(order, venue_order_id, Decimal(str(order.quantity)))

    def _generate_accepted(self, order, venue_order_id: VenueOrderId) -> None:
        ts = self._clock.timestamp_ns()
        self._client.generate_order_accepted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            ts_event=ts,
        )

    def _generate_fill(
        self,
        order,
        venue_order_id: VenueOrderId,
        fill_qty: Decimal,
    ) -> None:
        ts = self._clock.timestamp_ns()
        inst = self._get_instrument(order.instrument_id)
        make_qty = getattr(inst, "make_qty", None)

        last_qty = (
            make_qty(float(fill_qty))
            if callable(make_qty)
            else Quantity(float(fill_qty), precision=2)
        )

        raw_price = getattr(order, "price", None)
        if raw_price is None:
            last_px = Price.from_str("0.5")
        else:
            last_px = raw_price if isinstance(raw_price, Price) else Price.from_str(str(raw_price))

        # 生成唯一 trade_id
        trade_id = TradeId(f"MOCK-{order.client_order_id.value}-{ts}")

        self._client.generate_order_filled(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            venue_position_id=None,
            trade_id=trade_id,
            order_side=order.side,
            order_type=order.order_type,
            last_qty=last_qty,
            last_px=last_px,
            quote_currency=self._quote_currency,
            commission=Money(Decimal("0"), self._quote_currency),
            liquidity_side=LiquiditySide.TAKER,
            ts_event=ts,
        )

    def _generate_rejected(
        self,
        order,
        venue_order_id: VenueOrderId,
        reason: str,
    ) -> None:
        ts = self._clock.timestamp_ns()
        self._client.generate_order_rejected(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            reason=reason or "MOCK_REJECTED",
            ts_event=ts,
        )

    def _generate_canceled(
        self,
        order,
        venue_order_id: VenueOrderId,
        reason: str,
    ) -> None:
        ts = self._clock.timestamp_ns()
        self._client.generate_order_canceled(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            ts_event=ts,
        )

    def _generate_expired(self, order, venue_order_id: VenueOrderId) -> None:
        ts = self._clock.timestamp_ns()
        self._client.generate_order_expired(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            ts_event=ts,
        )

    def _get_instrument(self, instrument_id):
        cache = getattr(self._client, "_cache", None)
        if cache is None:
            return None
        instrument = getattr(cache, "instrument", None)
        return instrument(instrument_id) if callable(instrument) else None
