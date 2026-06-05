"""BoolExpr —— AND/OR/NOT + SignalRef 求值,不消费 transient。

对应用例:strategy-4.framework.expr.{1-6}
"""

from src.arbitrage.strategy.bool_expr import AndExpr
from src.arbitrage.strategy.bool_expr import NotExpr
from src.arbitrage.strategy.bool_expr import OrExpr
from src.arbitrage.strategy.bool_expr import SignalRef
from src.arbitrage.strategy.signals import SignalStore


def test_signal_ref_missing_is_false():
    """expr.1a: 信号缺 → False。"""
    s = SignalStore()
    assert SignalRef("live").eval(s) is False


def test_signal_ref_truthy_is_true():
    """expr.1b: 信号存在且 truthy → True。"""
    s = SignalStore(); s.set_persistent("live", True)
    assert SignalRef("live").eval(s) is True


def test_signal_ref_with_predicate():
    """expr.1c: 自定义谓词(例如 rebate > 阈值)。"""
    s = SignalStore(); s.set_transient("rebate", 0.03)
    expr = SignalRef("rebate", pred=lambda v: v is not None and v > 0.02)
    assert expr.eval(s) is True
    s2 = SignalStore(); s2.set_transient("rebate", 0.01)
    assert expr.eval(s2) is False


def test_and_expr_all_true():
    """expr.2: AND — 全 True 才 True。"""
    s = SignalStore(); s.set_persistent("a", True); s.set_persistent("b", True)
    assert AndExpr(SignalRef("a"), SignalRef("b")).eval(s) is True
    s.set_persistent("a", False)
    assert AndExpr(SignalRef("a"), SignalRef("b")).eval(s) is False


def test_or_expr_any_true():
    """expr.3: OR — 任一 True 即 True。"""
    s = SignalStore(); s.set_persistent("a", False); s.set_persistent("b", True)
    assert OrExpr(SignalRef("a"), SignalRef("b")).eval(s) is True
    s.set_persistent("b", False)
    assert OrExpr(SignalRef("a"), SignalRef("b")).eval(s) is False


def test_not_expr_inverts():
    """expr.4: NOT 取反。"""
    s = SignalStore(); s.set_persistent("a", True)
    assert NotExpr(SignalRef("a")).eval(s) is False
    s.set_persistent("a", False)
    assert NotExpr(SignalRef("a")).eval(s) is True


def test_nested_and_or_not():
    """expr.5: 嵌套 AND(a, OR(b, NOT(c)))。"""
    s = SignalStore()
    expr = AndExpr(SignalRef("a"), OrExpr(SignalRef("b"), NotExpr(SignalRef("c"))))
    # a=True, b=False, c=False → a ∧ (b ∨ ¬c) = T ∧ (F ∨ T) = T
    s.set_persistent("a", True); s.set_persistent("b", False); s.set_persistent("c", False)
    assert expr.eval(s) is True
    # a=True, b=False, c=True → a ∧ (F ∨ F) = F
    s.set_persistent("c", True)
    assert expr.eval(s) is False
    # a=False, ... → F
    s.set_persistent("a", False)
    assert expr.eval(s) is False


def test_bool_expr_eval_does_not_consume_transient():
    """expr.6: BoolExpr 经 store.peek,不消费 transient(关键不变量)。"""
    s = SignalStore(); s.set_transient("rebate", 0.03)
    expr = SignalRef("rebate", pred=lambda v: v is not None and v > 0)
    # 反复求值
    assert expr.eval(s) is True
    assert expr.eval(s) is True
    # 仍能 get 拿到原值(没被求值消费掉)
    assert s.get("rebate") == 0.03


def test_empty_and_is_true_empty_or_is_false():
    """边界:空 AND/OR 的语义(vacuous truth)。"""
    s = SignalStore()
    assert AndExpr().eval(s) is True          # all([]) = True
    assert OrExpr().eval(s) is False          # any([]) = False
