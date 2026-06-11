"""PairInFlightGate(synchronization.md §7,#84)单测:per-pair 串行闸。"""

from src.arbitrage.common.pair_inflight import PairInFlightGate

_MAX_HOLD = 60_000_000_000  # 60s in ns


def test_try_enter_second_same_pair_discarded():
    g = PairInFlightGate()
    assert g.try_enter("P", now_ns=0, max_hold_ns=_MAX_HOLD) is True
    # 同 pair 并发再进 → 放弃
    assert g.try_enter("P", now_ns=1, max_hold_ns=_MAX_HOLD) is False


def test_different_pairs_independent():
    g = PairInFlightGate()
    assert g.try_enter("P1", now_ns=0, max_hold_ns=_MAX_HOLD) is True
    assert g.try_enter("P2", now_ns=0, max_hold_ns=_MAX_HOLD) is True   # 不同 pair 可并发


def test_release_eval_when_no_fire_allows_reenter():
    g = PairInFlightGate()
    g.try_enter("P", now_ns=0, max_hold_ns=_MAX_HOLD)
    g.release_eval("P")                                  # 未 fire → 释放
    assert g.try_enter("P", now_ns=1, max_hold_ns=_MAX_HOLD) is True


def test_fire_handoff_held_through_execution_then_released():
    """fire 后 strategy 不释放;execution per-leg exec_started/finished,归 0 才释放。"""
    g = PairInFlightGate()
    g.try_enter("P", now_ns=0, max_hold_ns=_MAX_HOLD)    # strategy 评估入口
    # 已 fire → 双腿 session 启动
    g.exec_started("P")
    g.exec_started("P")
    # strategy finally 的 release_eval:exec_count>0 → no-op(不释放)
    g.release_eval("P")
    assert g.is_in_flight("P") is True
    assert g.try_enter("P", now_ns=1, max_hold_ns=_MAX_HOLD) is False    # 执行中,新机会放弃
    # 一腿结束 → 仍持有
    g.exec_finished("P")
    assert g.is_in_flight("P") is True
    # 末腿结束 → 计数归 0 → 释放
    g.exec_finished("P")
    assert g.is_in_flight("P") is False
    assert g.try_enter("P", now_ns=2, max_hold_ns=_MAX_HOLD) is True


def test_max_hold_self_heal_on_leak():
    """fire 了但一腿 session 都没起(全 deny)→ in-flight 泄漏 → 超 max_hold 自愈可重入。"""
    g = PairInFlightGate()
    g.try_enter("P", now_ns=0, max_hold_ns=_MAX_HOLD)
    # 模拟泄漏:无 exec_started、strategy 也没 release(fire=True 路径)
    # 未超 max_hold → 仍挡
    assert g.try_enter("P", now_ns=_MAX_HOLD - 1, max_hold_ns=_MAX_HOLD) is False
    # 超 max_hold → 视为陈旧,可重入(覆盖置位)
    assert g.try_enter("P", now_ns=_MAX_HOLD + 1, max_hold_ns=_MAX_HOLD) is True


def test_clear_all_wipes_inflight_and_exec_count():
    """§6.10 §7 兜底:clear_all 连 _inflight + _exec_count 一起清(max-hold 清不到 exec_count)。"""
    g = PairInFlightGate()
    g.try_enter("P1", now_ns=0, max_hold_ns=_MAX_HOLD)
    g.try_enter("P2", now_ns=0, max_hold_ns=_MAX_HOLD)
    g.exec_started("P1")                                 # 模拟泄漏的脏计数
    g.clear_all()
    assert g.is_in_flight("P1") is False and g.is_in_flight("P2") is False
    # exec_count 也清了:再 try_enter + 正常 exec 流程不被脏计数干扰
    assert g.try_enter("P1", now_ns=1, max_hold_ns=_MAX_HOLD) is True
    g.exec_started("P1")
    g.exec_finished("P1")
    assert g.is_in_flight("P1") is False                 # 计数干净,1→0 正常释放


def test_exec_finished_without_started_is_safe():
    """防御:exec_finished 多于 started 不崩、不负计数。"""
    g = PairInFlightGate()
    g.try_enter("P", now_ns=0, max_hold_ns=_MAX_HOLD)
    g.exec_started("P")
    g.exec_finished("P")
    g.exec_finished("P")                                 # 多余的一次
    assert g.is_in_flight("P") is False
