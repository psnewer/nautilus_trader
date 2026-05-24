"""
InstrumentRefresher Actor 测试(用例见 README.md)。

职责:周期触发 Provider.load_all_async / 失败/0 不 publish / on_save/on_load 持久化
refresh_interval / msgbus 命令运行时改值。

对应章节: refactor.md §5.2.2, §6.3;架构 architectures/discovery/architecture.md §3.3/§4.2/§4.3
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import TestClock
from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from src.arbitrage.discovery.events import InstrumentsRefreshed
from src.arbitrage.discovery.refresher import InstrumentRefresher
from src.arbitrage.discovery.refresher import InstrumentRefresherConfig
from src.arbitrage.discovery.refresher import _RuntimeDeps


class _StubProvider:
    """Provider 替身:get_all 返回 dict,load_all_async 可注入行为。"""

    def __init__(self, instruments=None, raises=None):
        self._instruments = instruments or {}
        self._raises = raises
        self.load_calls = 0

    def get_all(self):
        return self._instruments

    async def load_all_async(self, filters=None):
        self.load_calls += 1
        if self._raises is not None:
            raise self._raises


def _refresher(provider, interval=10.0, min_interval=5.0, venue="ORBITEXCH"):
    clock = TestClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    cache = TestComponentStubs.cache()
    portfolio = Portfolio(msgbus=msgbus, cache=cache, clock=clock)

    cfg = InstrumentRefresherConfig(
        venue=venue, refresh_interval_default=interval, min_interval=min_interval,
    )
    loop = asyncio.new_event_loop()
    deps = _RuntimeDeps(provider=provider, loop=loop)
    r = InstrumentRefresher(cfg, deps)
    r.register_base(portfolio=portfolio, msgbus=msgbus, cache=cache, clock=clock)
    return r, clock, msgbus


# ── 生命周期 / 调度 ────────────────────────────────────────────────
def test_on_start_schedules_first_alert():
    """discovery-2.1: on_start 后 NT clock 上挂着 interval 后的 alert。"""
    r, clock, _ = _refresher(_StubProvider(), interval=10.0)
    r.on_start()
    assert clock.next_time_ns(f"instrument_refresher:ORBITEXCH") == secs_to_nanos(10.0)


def test_on_stop_cancels_alert():
    """discovery-2.2: on_stop 取消 timer(不卡住关停)。"""
    r, clock, _ = _refresher(_StubProvider())
    r.on_start()
    r.on_stop()
    assert clock.next_time_ns(f"instrument_refresher:ORBITEXCH") == 0


# ── 持久化(on_save/on_load,Q6)────────────────────────────────────
def test_on_save_emits_current_interval():
    """discovery-2.3: on_save 序列化当前 refresh_interval(NT 持久化通道)。"""
    r, *_ = _refresher(_StubProvider(), interval=12.0)
    state = r.on_save()
    assert state == {"refresh_interval": b"12.0"}


def test_on_load_restores_interval():
    """discovery-2.4: on_load 从字节状态恢复 interval。"""
    r, *_ = _refresher(_StubProvider(), interval=30.0)
    r.on_load({"refresh_interval": b"45.0"})
    assert r._interval_secs == 45.0


def test_on_load_clamps_to_min():
    """discovery-2.5: 持久值低于 min_interval → 夹到下界(防过频拉)。"""
    r, *_ = _refresher(_StubProvider(), interval=30.0, min_interval=5.0)
    r.on_load({"refresh_interval": b"1.0"})
    assert r._interval_secs == 5.0


def test_on_load_corrupt_keeps_default():
    """discovery-2.6: 损坏的持久值 → 保留 default、不抛(不让坏数据炸启动)。"""
    r, *_ = _refresher(_StubProvider(), interval=30.0)
    r.on_load({"refresh_interval": b"not-a-number"})
    assert r._interval_secs == 30.0


# ── 运行时改值(msgbus 命令,Q3)────────────────────────────────────
def test_runtime_command_updates_interval():
    """discovery-2.7: msgbus 命令 `config.{venue}.refresh_interval` 改值;next 重排读新值。"""
    r, clock, msgbus = _refresher(_StubProvider(), interval=10.0)
    r.on_start()
    msgbus.publish(topic="config.ORBITEXCH.refresh_interval", msg=20.0)
    assert r._interval_secs == 20.0
    # 立刻重排一次看读新值
    clock.cancel_timer(f"instrument_refresher:ORBITEXCH")
    r._schedule_next()
    assert clock.next_time_ns(f"instrument_refresher:ORBITEXCH") == secs_to_nanos(20.0)


def test_runtime_command_clamps_to_min():
    """discovery-2.8: 命令值小于 min_interval → 夹到下界。"""
    r, _, msgbus = _refresher(_StubProvider(), interval=10.0, min_interval=5.0)
    r.on_start()
    msgbus.publish(topic="config.ORBITEXCH.refresh_interval", msg=1.0)
    assert r._interval_secs == 5.0


# ── tick 行为(成功 publish / 0 / 异常)──────────────────────────────
@pytest.mark.asyncio
async def test_tick_publishes_on_success():
    """discovery-2.9: load_all_async 成功 + count>0 → publish InstrumentsRefreshed + 重排。"""
    p = _StubProvider(instruments={"i1": object(), "i2": object()})
    r, clock, _ = _refresher(p, interval=10.0)
    r._running = True  # 模拟 on_start 已跑
    published = []
    r.publish_data = lambda *, data_type, data: published.append(data)

    await r._tick()

    assert p.load_calls == 1
    assert len(published) == 1 and isinstance(published[0], InstrumentsRefreshed)
    assert published[0].venue == "ORBITEXCH" and published[0].count == 2
    assert clock.next_time_ns(f"instrument_refresher:ORBITEXCH") > 0  # 重排


@pytest.mark.asyncio
async def test_tick_skips_publish_when_zero_count():
    """discovery-2.10: provider 0 instrument → 不 publish,仍重排。"""
    p = _StubProvider(instruments={})
    r, clock, _ = _refresher(p)
    published = []
    r.publish_data = lambda **k: published.append(k)
    await r._tick()
    assert published == []
    assert clock.next_time_ns(f"instrument_refresher:ORBITEXCH") > 0


@pytest.mark.asyncio
async def test_tick_swallows_provider_error_no_publish_reschedules():
    """discovery-2.11: provider 抛 → 不 publish + 不抛,仍重排(Q4 静默失败 + 不卡死)。"""
    p = _StubProvider(raises=RuntimeError("scraper failed"))
    r, clock, _ = _refresher(p)
    published = []
    r.publish_data = lambda **k: published.append(k)
    await r._tick()                                    # 不抛
    assert published == []
    assert clock.next_time_ns(f"instrument_refresher:ORBITEXCH") > 0  # 仍重排
