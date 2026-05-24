"""
InstrumentRefresher —— NT `Actor` 子类,周期触发 `provider.load_all_async()` →
instrument 入 cache → publish `data:InstrumentsRefreshed.{venue}`(成功 + count>0 才 publish,Q4)。

设计见 `docs/arbitrage/architectures/discovery/architecture.md §3.3 / §4.2 / §4.3`。

节奏对齐 §6.8.4.5:NT `Clock` 自重排 one-shot alert,callback `try/finally`(try 跑 +
publish;finally 按当前 `_interval` 重排);异常路径也重排(单次失败不卡死,Q4 不发事件)。
`refresh_interval` 经 `on_save`/`on_load` 经 NT 持久化(Q3/Q6);msgbus 命令
`config.{venue}.refresh_interval` 运行时改值即时生效。
"""

from __future__ import annotations

from dataclasses import dataclass

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.actor import ActorConfig
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.core.datetime import secs_to_nanos

from src.arbitrage.discovery.events import InstrumentsRefreshed


_ALERT_PREFIX = "instrument_refresher:"


class InstrumentRefresherConfig(ActorConfig, frozen=True, kw_only=True):
    """`venue` 用于 alert/事件命名与 config 命令路由;`provider` 注入(factory 不走 config)。"""

    venue: str                                  # "POLYMARKET" / "ORBITEXCH"
    refresh_interval_default: float = 30.0      # 秒
    min_interval: float = 5.0                   # 运行时改值下界


@dataclass(slots=True)
class _RuntimeDeps:
    """构造时注入(非 msgspec config 可放对象的字段)。`loop` 用于 sync clock 回调里 `create_task`。"""

    provider: InstrumentProvider
    loop: object  # asyncio.AbstractEventLoop;Refresher 与 LiveExecutionClient 同模式,不复用 NT register_executor 的线程池


class InstrumentRefresher(Actor):
    def __init__(self, config: InstrumentRefresherConfig, deps: _RuntimeDeps) -> None:
        super().__init__(config=config)
        self._venue = config.venue
        self._provider = deps.provider
        self._loop = deps.loop
        self._interval_secs: float = config.refresh_interval_default
        self._min_interval: float = config.min_interval
        self._alert_name = f"{_ALERT_PREFIX}{self._venue}"

    # ── 生命周期 ──────────────────────────────────────────────────────
    def on_start(self) -> None:
        # 订 msgbus 命令改 interval(运行时面板)
        self._msgbus.subscribe(
            topic=f"config.{self._venue}.refresh_interval",
            handler=self._on_interval_command,
        )
        self._schedule_next()

    def on_stop(self) -> None:
        self._cancel_alert()

    # ── 持久化(NT on_save/on_load,Q3/Q6)──────────────────────────────
    def on_save(self) -> dict[str, bytes]:
        return {"refresh_interval": str(self._interval_secs).encode()}

    def on_load(self, state: dict[str, bytes]) -> None:
        raw = state.get("refresh_interval")
        if raw is not None:
            try:
                self._interval_secs = max(self._min_interval, float(raw.decode()))
            except (TypeError, ValueError):
                pass  # 持久值损坏 → 用 default,不抛

    # ── 运行时改值(msgbus 命令)──────────────────────────────────────
    def _on_interval_command(self, msg) -> None:
        """msg 兼容:`float|int`(直接秒数)或带 `value` 字段的 dict。"""
        try:
            new_value = float(msg["value"] if isinstance(msg, dict) else msg)
        except (TypeError, ValueError, KeyError):
            self._log.warning(f"{self._venue}: bad refresh_interval command: {msg!r}")
            return
        self._interval_secs = max(self._min_interval, new_value)
        self._log.info(f"{self._venue}: refresh_interval={self._interval_secs}s")

    # ── 周期循环(NT clock 自重排,§6.8.4.5 同节奏)──────────────────
    def _schedule_next(self) -> None:
        self._cancel_alert()
        self._clock.set_time_alert_ns(
            name=self._alert_name,
            alert_time_ns=self._clock.timestamp_ns() + secs_to_nanos(self._interval_secs),
            callback=self._on_alert,
        )

    def _on_alert(self, event) -> None:
        # sync callback → 异步 tick(Provider load_all_async async)
        self._loop.create_task(self._tick())

    async def _tick(self) -> None:
        try:
            count_before = len(self._provider.get_all())
            try:
                await self._provider.load_all_async()
            except Exception as e:  # noqa: BLE001 — Q4:抛 → 不 publish,下轮重试
                self._log.error(f"{self._venue}: refresh failed: {e}")
                return
            count_after = len(self._provider.get_all())
            delta = count_after - count_before  # 本轮新增/净变动
            if count_after == 0:
                self._log.warning(f"{self._venue}: refresh produced 0 instruments — skip publish")
                return
            self.publish_data(
                data_type=_event_data_type(),
                data=InstrumentsRefreshed(
                    ts_event=self._clock.timestamp_ns(),
                    ts_init=self._clock.timestamp_ns(),
                    venue=self._venue,
                    count=count_after,
                ),
            )
            self._log.info(f"{self._venue}: refresh ok count={count_after} (Δ{delta:+d})")
        finally:
            self._schedule_next()  # 异常/失败也重排(不永久卡死)

    def _cancel_alert(self) -> None:
        try:
            self._clock.cancel_timer(self._alert_name)
        except (KeyError, ValueError):
            pass


def _event_data_type():
    from nautilus_trader.model.data import DataType
    return DataType(InstrumentsRefreshed)
