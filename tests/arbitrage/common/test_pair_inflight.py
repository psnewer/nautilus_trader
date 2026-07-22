"""PairInFlightGate(synchronization.md §7,#84;#261 收窄为 strategy 单层)单测。

**#261 后本闸只管一件事:同一 pair 不并发评估。** 它不再进入执行段 —— 原先的
`exec_started`/`exec_finished`/`_exec_count` 与"已 fire 就不释放"的交接判据全部删除,
因为跨组件传递所有权需要判据,而判据会漏(#260 查出四类同源泄漏)。
全局 ≤1 执行改由 `ArbLiveExecutionEngine` barrier 用派生态判定,见 `test_engine_barrier.py`。
"""

import pytest

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


def test_release_allows_reenter():
    g = PairInFlightGate()
    g.try_enter("P")
    g.release("P")
    assert g.try_enter("P") is True


def test_release_is_unconditional():
    """#261:没有"已 fire 则不释放"的例外 —— 评估 task 结束就释放,无论有没有下单。

    旧行为(`release_eval` 在 `exec_count>0` 时 no-op)是为跨组件交接服务的,正是
    #260 泄漏的根源:交接判据一旦漏判,闸就永久不放。
    """
    g = PairInFlightGate()
    g.try_enter("P")
    g.release("P")
    assert g.is_in_flight("P") is False


def test_release_without_enter_is_safe():
    """防御:未 acquire 就 release 不崩(排程失败分支可能走到)。"""
    g = PairInFlightGate()
    g.release("P")                                       # 不抛
    assert g.is_in_flight("P") is False


def test_repeated_release_is_idempotent():
    g = PairInFlightGate()
    g.try_enter("P")
    g.release("P")
    g.release("P")
    assert g.is_in_flight("P") is False
    assert g.try_enter("P") is True


def test_execution_apis_removed():
    """#261:执行段 API 必须真的消失,不能留下会误导的空壳。"""
    g = PairInFlightGate()
    for name in ("exec_started", "exec_finished", "release_eval"):
        assert not hasattr(g, name), f"{name} 应已随 #261 删除"
    with pytest.raises(AttributeError):
        _ = g._exec_count
