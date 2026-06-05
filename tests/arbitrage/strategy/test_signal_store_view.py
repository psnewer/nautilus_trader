"""Slice 9(#49):`SignalStore.view(pair_id)` per-pair 子视图(P3 隔离)。

API 详见 `src/arbitrage/strategy/signals.py:SignalStore.view`。
"""

from src.arbitrage.strategy.signals import SignalStore


def test_view_writes_namespaced_into_underlying_store():
    """view.set_persistent("k", v) 应实际写 root store 的 `k.{pair_id}`。"""
    store = SignalStore()
    store.view("pair_A").set_persistent("in_play", True)
    # 内部 namespaced key
    assert store.peek("in_play.pair_A") is True
    # 同名 root key 不存在(隔离)
    assert store.peek("in_play") is None


def test_view_reads_only_its_own_namespace():
    """view.peek("k") 只看 namespace 内的值。"""
    store = SignalStore()
    store.view("pair_A").set_persistent("in_play", True)
    store.view("pair_B").set_persistent("in_play", False)
    assert store.view("pair_A").peek("in_play") is True
    assert store.view("pair_B").peek("in_play") is False
    # 不互相污染


def test_view_get_consumes_transient_only_in_namespace():
    store = SignalStore()
    store.view("pA").set_transient("rebate", 0.05)
    store.view("pB").set_transient("rebate", 0.07)

    assert store.view("pA").get("rebate") == 0.05  # consume A
    assert store.view("pA").get("rebate") is None  # A 消耗后 None
    assert store.view("pB").get("rebate") == 0.07  # B 未受影响


def test_view_has_namespaced():
    store = SignalStore()
    store.view("pA").set_persistent("k", 1)
    assert store.view("pA").has("k") is True
    assert store.view("pB").has("k") is False


def test_view_clear_persistent_namespaced():
    store = SignalStore()
    store.view("pA").set_persistent("k", 1)
    store.view("pA").clear_persistent("k")
    assert store.view("pA").peek("k") is None


def test_root_and_view_coexist():
    """跨 pair 全局信号走 root store(`view` 不用),per-pair 走 view —— 两者命名空间独立。"""
    store = SignalStore()
    store.set_persistent("global_pause", True)               # 全局
    store.view("pair_A").set_persistent("in_play", True)     # per-pair

    assert store.peek("global_pause") is True
    assert store.view("pair_A").peek("in_play") is True
    # root 看不到 view 的 k(因为 root key 是 "in_play",view 写的是 "in_play.pair_A")
    assert store.peek("in_play") is None
