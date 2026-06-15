"""PairInFlightGate(synchronization.md §7,#84)单测:per-pair 串行闸。

#105 ②:无 max-hold 自愈、无 clear_all 兜底 —— in-flight 出口靠结构保证(barrier deny/timeout
出口 + session `exec_started`↔watchdog 原子)。本闸只管 try_enter / release_eval / exec_count。
"""

from src.arbitrage.common.pair_inflight import PairInFlightGate


def test_try_enter_second_same_pair_discarded():
    g = PairInFlightGate()
    assert g.try_enter("P") is True
    # 同 pair 并发再进 → 放弃
    assert g.try_enter("P") is False


def test_different_pairs_independent():
    g = PairInFlightGate()
    assert g.try_enter("P1") is True
    assert g.try_enter("P2") is True                      # 不同 pair 可并发


def test_release_eval_when_no_fire_allows_reenter():
    g = PairInFlightGate()
    g.try_enter("P")
    g.release_eval("P")                                  # 未 fire → 释放
    assert g.try_enter("P") is True


def test_fire_handoff_held_through_execution_then_released():
    """fire 后 strategy 不释放;execution per-leg exec_started/finished,归 0 才释放。"""
    g = PairInFlightGate()
    g.try_enter("P")                                     # strategy 评估入口
    # 已 fire → 双腿 session 启动
    g.exec_started("P")
    g.exec_started("P")
    # strategy finally 的 release_eval:exec_count>0 → no-op(不释放)
    g.release_eval("P")
    assert g.is_in_flight("P") is True
    assert g.try_enter("P") is False                     # 执行中,新机会放弃
    # 一腿结束 → 仍持有
    g.exec_finished("P")
    assert g.is_in_flight("P") is True
    # 末腿结束 → 计数归 0 → 释放
    g.exec_finished("P")
    assert g.is_in_flight("P") is False
    assert g.try_enter("P") is True


def test_release_eval_after_fire_is_noop():
    """fire 后(exec_count>0)strategy finally 的 release_eval 不得提前清 in-flight。"""
    g = PairInFlightGate()
    g.try_enter("P")
    g.exec_started("P")
    g.release_eval("P")                                  # exec_count>0 → no-op
    assert g.is_in_flight("P") is True
    g.exec_finished("P")
    assert g.is_in_flight("P") is False                  # 归 0 才释放


def test_exec_finished_without_started_is_safe():
    """防御:exec_finished 多于 started 不崩、不负计数。"""
    g = PairInFlightGate()
    g.try_enter("P")
    g.exec_started("P")
    g.exec_finished("P")
    g.exec_finished("P")                                 # 多余的一次
    assert g.is_in_flight("P") is False
