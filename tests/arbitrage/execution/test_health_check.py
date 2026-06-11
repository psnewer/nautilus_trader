"""HealthCheckLoop —— 自重排节奏 / 每实例可设间隔 / 执行让路 / publish health_check.*。"""

import asyncio
import logging

from nautilus_trader.common.component import TestClock
from nautilus_trader.core.datetime import secs_to_nanos

from src.arbitrage.execution.health_check import HealthCheckLoop

LOG = logging.getLogger("hc-test")


class FakeLoop:
    def __init__(self):
        self.tasks = []

    def create_task(self, coro):
        self.tasks.append(coro)
        return coro


class FakeMsgbus:
    def __init__(self):
        self.published = []

    def publish(self, topic, msg):
        self.published.append(topic)


def _make(clock=None, interval=5.0, active=False, run=None, name="health_check:TEST"):
    clock = clock or TestClock()
    msgbus = FakeMsgbus()
    calls = {"run": 0}

    async def _default_run():
        calls["run"] += 1

    interval_box = {"v": interval}
    active_box = {"v": active}

    hc = HealthCheckLoop(
        name=name,
        clock=clock,
        msgbus=msgbus,
        loop=FakeLoop(),
        log=LOG,
        interval_secs_provider=lambda: interval_box["v"],
        is_execution_active=lambda: active_box["v"],
        run_check=run or _default_run,
    )
    return hc, clock, msgbus, calls, interval_box, active_box


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


# ── 调度 / 间隔 ────────────────────────────────────────────────────
def test_start_schedules_at_interval():
    hc, clock, *_ = _make(interval=5.0)
    hc.start()
    assert clock.next_time_ns("health_check:TEST") == secs_to_nanos(5.0)


def test_interval_reread_each_reschedule():
    hc, clock, _, _, interval_box, _ = _make(interval=5.0)
    hc._running = True
    hc._schedule_next()
    assert clock.next_time_ns("health_check:TEST") == secs_to_nanos(5.0)
    interval_box["v"] = 12.0          # 运行时改值
    clock.cancel_timer("health_check:TEST")
    hc._schedule_next()
    assert clock.next_time_ns("health_check:TEST") == secs_to_nanos(12.0)  # 重读生效


def test_pm_oe_separate_intervals():
    clock = TestClock()
    pm, _, *_ = _make(clock=clock, interval=8.0, name="health_check:POLYMARKET")
    oe, _, *_ = _make(clock=clock, interval=20.0, name="health_check:ORBITEXCH")
    pm.start()
    oe.start()
    assert clock.next_time_ns("health_check:POLYMARKET") == secs_to_nanos(8.0)
    assert clock.next_time_ns("health_check:ORBITEXCH") == secs_to_nanos(20.0)


def test_trigger_now_sets_immediate_alert():
    hc, clock, *_ = _make(interval=5.0)
    hc.start()
    clock.set_time(secs_to_nanos(2.0)) if hasattr(clock, "set_time") else None
    hc.trigger_now()
    assert clock.next_time_ns("health_check:TEST") == clock.timestamp_ns()


def test_stop_marks_not_running():
    hc, clock, *_ = _make()
    hc.start()
    hc.stop()
    assert hc._running is False


# ── tick 行为 ──────────────────────────────────────────────────────
def test_tick_runs_check_and_publishes():
    hc, clock, msgbus, calls, *_ = _make(interval=5.0, active=False)
    hc._running = True
    _run(hc._tick())
    assert calls["run"] == 1
    assert msgbus.published == ["health_check.started", "health_check.finished"]
    assert clock.next_time_ns("health_check:TEST") == secs_to_nanos(5.0)  # finally 重排


def test_tick_skips_when_execution_active():
    hc, clock, msgbus, calls, _, active_box = _make(active=True)
    hc._running = True
    _run(hc._tick())
    assert calls["run"] == 0                  # 执行在飞 → 不跑检查
    assert msgbus.published == []             # 不 publish started/finished
    assert clock.next_time_ns("health_check:TEST") == secs_to_nanos(5.0)  # 但仍重排


def test_tick_swallows_check_error_and_reschedules():
    async def _boom():
        raise RuntimeError("pull failed")

    hc, clock, msgbus, *_ = _make(run=_boom)
    hc._running = True
    _run(hc._tick())                          # 不抛
    assert msgbus.published == ["health_check.started", "health_check.finished"]  # finished 仍发
    assert clock.next_time_ns("health_check:TEST") == secs_to_nanos(5.0)          # 仍重排


def test_tick_no_reschedule_after_stop():
    hc, clock, msgbus, *_ = _make()
    hc._running = False                       # 已 stop
    _run(hc._tick())
    assert clock.next_time_ns("health_check:TEST") == 0  # 无 timer 重排


# ── 全链路:clock 触发 → create_task ───────────────────────────────
def test_alert_fires_creates_tick_task():
    hc, clock, msgbus, calls, *_ = _make(interval=5.0)
    hc.start()
    handlers = clock.advance_time(secs_to_nanos(6.0))
    for h in handlers:
        h.handle()                            # → _on_alert → loop.create_task(_tick)
    assert len(hc._loop.tasks) == 1
    _run(hc._loop.tasks[0])                    # 执行 tick
    assert calls["run"] == 1
