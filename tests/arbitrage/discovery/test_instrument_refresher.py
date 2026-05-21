"""
InstrumentRefresher Actor 测试(用例见 README.md)。

InstrumentRefresher 是 Step 2 引入的独立 Actor(每 venue 一个),职责:
- 周期触发 InstrumentProvider.load_all_async
- 持有 refresh_interval 并通过 NT on_save/on_load 持久化(Q6)
- 通过 MessageBus 命令 config.{venue}.refresh_interval 运行时调整(Q3)
- 完成后 publish InstrumentsRefreshed 事件(Q4)
- 失败时静默不发事件(让 MatchingActor 自然 gate 住)

对应章节: refactor.md §5.2.2, §6.3
"""

import pytest


@pytest.mark.skip(reason="not implemented; impl pending Step 2 execution")
def test_refresher_periodic_trigger():
    """
    discovery-2.1: Refresher 周期触发

    refresh_interval=5s,跑 12s,验证 provider.load_all_async 被调 ~2 次。
    """


@pytest.mark.skip(reason="not implemented")
def test_refresher_runtime_mutable_interval_via_msgbus():
    """
    discovery-2.2: refresh_interval 通过 MessageBus 运行时可变 (Q3)

    初始 30s,publish "config.{venue}.refresh_interval=5",等 7s 验证 load_all_async
    至少触发 1 次(说明已切到 5s)。
    """


@pytest.mark.skip(reason="not implemented")
def test_refresher_interval_persisted_via_nt_on_save_load():
    """
    discovery-2.3: refresh_interval 通过 NT on_save/on_load 持久化 (Q6)

    改 interval=60 → 触发 NT save → 验 Redis 有 actor state →
    重启 Refresher → on_load 恢复 → self._refresh_interval == 60(不是 default)。
    """


@pytest.mark.skip(reason="not implemented")
def test_refresher_publishes_instruments_refreshed_on_success():
    """
    discovery-2.4: 成功 refresh 发事件 (Q4)

    订阅者订 DataType(InstrumentsRefreshed),等一次 refresh,
    验证收到一条 InstrumentsRefreshed,字段 venue / count / ts_init 正确。
    """


@pytest.mark.skip(reason="not implemented")
def test_refresher_silent_on_failure():
    """
    discovery-2.5: refresh 失败时不发事件 (Q4)

    模拟 provider.load_all_async 抛异常,验证不 publish InstrumentsRefreshed,
    日志记录,下周期照常尝试。
    """


@pytest.mark.skip(reason="not implemented")
def test_refresher_min_interval_clamp():
    """
    discovery-2.6: MIN_INTERVAL 强制下限 (Q3)

    publish "config.{venue}.refresh_interval=0",验证内部 clamp 到 MIN_INTERVAL。
    避免误设 0 把 venue 刷挂。
    """


@pytest.mark.skip(reason="not implemented")
def test_pm_oe_refreshers_isolation():
    """
    discovery-2.7: 双 venue Refresher 隔离

    PM Refresher 抛异常,验证 OE Refresher 继续正常发 InstrumentsRefreshed,
    不互相影响。
    """
