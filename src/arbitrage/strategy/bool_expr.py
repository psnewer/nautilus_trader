"""
BoolExpr —— `Condition.self_hits` 的布尔表达式 DSL(Q21 / Q10-b)。

支持 AND/OR/NOT 嵌套;叶子是 `SignalRef("live")` 等(读 `SignalStore.peek`,**不消费** transient
—— 求值不能因为看一眼就把信号清掉,消费由 Action 内部用 `store.get`)。

示例:`AndExpr(SignalRef("live"), OrExpr(SignalRef("home_winning"), NotExpr(SignalRef("draw"))))`
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from typing import Callable

from src.arbitrage.strategy.signals import SignalStore


class BoolExpr(ABC):
    """Q21 self_hits 的布尔表达式基类。"""

    @abstractmethod
    def eval(self, store: SignalStore) -> bool:
        """求值,read-only(透过 store.peek,不消费 transient)。"""


class SignalRef(BoolExpr):
    """叶子:读 `store.peek(key)`,可选谓词;无谓词时取 truthy。"""

    def __init__(self, key: str, pred: Callable[[object], bool] | None = None) -> None:
        self.key = key
        self.pred = pred

    def eval(self, store: SignalStore) -> bool:
        value = store.peek(self.key)
        if self.pred is not None:
            return bool(self.pred(value))
        return bool(value)


class AndExpr(BoolExpr):
    """全 True 才 True;空(无子)→ 默认 True(空 AND = vacuous truth,跟 `all([])` 一致)。"""

    def __init__(self, *exprs: BoolExpr) -> None:
        self.exprs = exprs

    def eval(self, store: SignalStore) -> bool:
        return all(e.eval(store) for e in self.exprs)


class OrExpr(BoolExpr):
    """任一 True 即 True;空 → 默认 False(跟 `any([])` 一致)。"""

    def __init__(self, *exprs: BoolExpr) -> None:
        self.exprs = exprs

    def eval(self, store: SignalStore) -> bool:
        return any(e.eval(store) for e in self.exprs)


class NotExpr(BoolExpr):
    def __init__(self, expr: BoolExpr) -> None:
        self.expr = expr

    def eval(self, store: SignalStore) -> bool:
        return not self.expr.eval(store)
