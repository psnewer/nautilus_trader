"""
HealthCheckLoop —— OE/PM 共用的健康检查循环节奏(§4.3 / §6.8.4.5)。

NT `Clock` 自重排 one-shot alert:callback(sync,来自 clock)→ `create_task(_tick)`;
`_tick`(async)try 跑检查 + finally 按**当前** interval 重排下次 alert。无 `asyncio.Event` /
`monotonic` / block-unblock。关停 `stop()` 取消 timer。

**间隔分别可设(用户 2026-05-22)**:`interval_secs_provider` 为**每实例独立**的 callable,
每次重排时重读 → PM/OE 各传各的配置、且支持运行时改值即时生效。

**执行 ⊥ 健康检查互斥(Q19/§6.10)**:`is_execution_active` callable 抽象"执行在飞"判定 ——
PM(健康检查与 session 同在 ExecClient)直接传 `lambda: self._execution_active`;OE(健康检查
在 DataClient,与 ExecClient 不同对象)传一个由 msgbus 订阅 `execution.*` 维护的 ref-count 标志。
执行在飞 → 整个 tick 跳过(但仍重排下次)。

宿主只需提供:`clock` / `msgbus` / `loop` / `log` + 三个 callable(interval / is_active / run_check)。
`run_check` 是宿主的真实检查(PM:拉 positions/orders→reports + settlement + leg_settled;
OE:页面 reload→reports),async。
"""

from __future__ import annotations

from typing import Awaitable
from typing import Callable

from nautilus_trader.core.datetime import secs_to_nanos


class HealthCheckLoop:
    def __init__(
        self,
        *,
        name: str,
        clock,
        msgbus,
        loop,
        log,
        interval_secs_provider: Callable[[], float],
        is_execution_active: Callable[[], bool],
        run_check: Callable[[], Awaitable[None]],
    ) -> None:
        self._name = name  # 唯一 alert 名,如 "health_check:POLYMARKET"
        self._clock = clock
        self._msgbus = msgbus
        self._loop = loop
        self._log = log
        self._interval_secs_provider = interval_secs_provider
        self._is_execution_active = is_execution_active
        self._run_check = run_check
        self._running = False

    def start(self) -> None:
        self._running = True
        self._schedule_next()

    def stop(self) -> None:
        self._running = False
        self._cancel()

    def trigger_now(self) -> None:
        """外部事件请求立即检查(下一轮 loop 仍按 interval 重排)。"""
        if not self._running:
            return
        self._cancel()
        self._clock.set_time_alert_ns(
            name=self._name,
            alert_time_ns=self._clock.timestamp_ns(),
            callback=self._on_alert,
        )

    # ── clock 回调(sync)→ 异步 tick ─────────────────────────────────
    def _on_alert(self, event) -> None:
        self._loop.create_task(self._tick())

    async def _tick(self) -> None:
        try:
            if self._is_execution_active():
                self._log.debug(f"{self._name}: skipped (execution active)")
                return
            self._msgbus.publish(topic="health_check.started", msg={"source": self._name})
            try:
                await self._run_check()
            except Exception as e:  # noqa: BLE001 — 单次检查失败不打断循环(下轮重试)
                self._log.error(f"{self._name}: health check error: {e}")
            finally:
                self._msgbus.publish(topic="health_check.finished", msg={"source": self._name})
        finally:
            if self._running:
                self._schedule_next()  # 异常路径也重排,避免一次失败永久卡死(§6.8.4.5)

    def _schedule_next(self) -> None:
        interval = self._interval_secs_provider()  # 每次重读 → 运行时改值即时生效
        self._clock.set_time_alert_ns(
            name=self._name,
            alert_time_ns=self._clock.timestamp_ns() + secs_to_nanos(interval),
            callback=self._on_alert,
        )

    def _cancel(self) -> None:
        try:
            self._clock.cancel_timer(self._name)
        except (KeyError, ValueError):
            pass  # 无 pending timer(已 fire / 未起)
