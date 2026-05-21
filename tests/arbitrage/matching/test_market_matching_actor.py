"""
MarketMatchingActor 测试(摘要)。详细用例待 Step 3 启动时展开。

MarketMatchingActor 触发逻辑(锁定):
- 订阅 DataType(InstrumentsRefreshed)
- 维护 _last_success: dict[Venue, ts]
- 两家 venue 都在 fresh_window(2 × refresh_interval)内才触发匹配
- publish MatchedPair / MatchedPairRemoved 通过 MessageBus

对应章节: refactor.md §5.3
"""

import pytest


@pytest.mark.skip(reason="not implemented; impl pending Step 3")
def test_actor_triggers_when_both_venues_recent():
    """
    matching-3.1: 两家都有近期 refresh 时触发

    依次收到 PM / OE 的 InstrumentsRefreshed(都在 fresh_window 内),
    第二条到达后立即触发 _do_match,publish MatchedPair。
    """


@pytest.mark.skip(reason="not implemented")
def test_actor_gates_on_single_venue_missing():
    """
    matching-3.2: 单 venue 缺失时 gate 住 (Q4)

    只收到 PM 的 InstrumentsRefreshed,OE 从未到达。
    fresh_window 过完后 _do_match 从未被调用,无 MatchedPair publish。
    """


@pytest.mark.skip(reason="not implemented")
def test_actor_gates_on_stale_window():
    """
    matching-3.x: 一家长期失败,另一家不会用 stale 数据匹配 (Q5)

    PM 持续成功 refresh,OE 在 t=0 之后停发事件。验证:
    - t < 2*interval 内 OE 还"近期",会触发匹配
    - t >= 2*interval 后 OE 不再"近期",PM 自己的事件不再触发匹配
    """
