"""SignalStore —— 双状态(persistent 写后保留 / transient 用后即清)。

对应用例:strategy-4.framework.store.{1-4}
"""

from src.arbitrage.strategy.signals import SignalStore


def test_persistent_set_then_peek_get_keep_value():
    """store.1: persistent 写后多次 peek/get 都拿到值(不消费)。"""
    s = SignalStore()
    s.set_persistent("live", True)
    assert s.peek("live") is True
    assert s.get("live") is True
    assert s.get("live") is True               # persistent 不消费


def test_transient_peek_no_consume_get_consumes():
    """store.2: transient — peek 不消费;get 消费一次。"""
    s = SignalStore()
    s.set_transient("rebate", 0.025)
    assert s.peek("rebate") == 0.025
    assert s.peek("rebate") == 0.025           # 多次 peek 仍在
    assert s.get("rebate") == 0.025            # 消费
    assert s.get("rebate") is None             # 用后即清
    assert s.peek("rebate") is None


def test_clear_persistent_removes_key():
    """store.3: clear_persistent 删除该 key;不存在的 key 也不抛。"""
    s = SignalStore()
    s.set_persistent("live", True)
    s.clear_persistent("live")
    assert s.peek("live") is None
    s.clear_persistent("nonexistent")          # 不抛


def test_transient_overrides_persistent_in_reads():
    """store.4: 同 key 同时存在 → 读优先 transient(更新鲜),get 消费 transient 后回退 persistent。"""
    s = SignalStore()
    s.set_persistent("rebate", 0.01)           # 旧值
    s.set_transient("rebate", 0.02)            # 新值

    assert s.peek("rebate") == 0.02            # 优先 transient
    assert s.get("rebate") == 0.02             # 消费 transient
    assert s.get("rebate") == 0.01             # 回落 persistent(不再消费)
    assert s.peek("rebate") == 0.01


def test_missing_key_returns_none():
    s = SignalStore()
    assert s.peek("nope") is None
    assert s.get("nope") is None
    assert s.has("nope") is False


def test_has_covers_both_stores():
    s = SignalStore()
    s.set_persistent("a", 1)
    s.set_transient("b", 2)
    assert s.has("a") and s.has("b") and not s.has("c")
