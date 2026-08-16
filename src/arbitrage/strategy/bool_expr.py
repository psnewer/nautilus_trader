"""`Condition.self_hits` 的当前状态布尔查询 DSL。

支持 AND/OR/NOT 嵌套；普通叶子只读当前 `EvalContext`。`head/reverse` 是受控例外：
叶子命中时更新 StrategyRuntimeStore 中的动态基准，不产生下单等执行副作用。
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from src.arbitrage.strategy.condition import EvalContext


class BoolExpr(ABC):
    """Q21 self_hits 的布尔表达式基类。"""

    @abstractmethod
    def eval(self, ctx: EvalContext) -> bool:
        """基于本轮上下文求值。"""


class StateQuery(BoolExpr):
    """self_hits 叶子：查询当前上下文；特定转态查询可在命中时更新运行时变量。"""

    @abstractmethod
    def matches(self, ctx: EvalContext) -> bool:
        """返回当前状态是否满足条件。"""

    def eval(self, ctx: EvalContext) -> bool:
        return bool(self.matches(ctx))


class AndExpr(BoolExpr):
    """全 True 才 True;空(无子)→ 默认 True(空 AND = vacuous truth,跟 `all([])` 一致)。"""

    def __init__(self, *exprs: BoolExpr) -> None:
        self.exprs = exprs

    def eval(self, ctx: EvalContext) -> bool:
        return all(e.eval(ctx) for e in self.exprs)


class OrExpr(BoolExpr):
    """任一 True 即 True;空 → 默认 False(跟 `any([])` 一致)。"""

    def __init__(self, *exprs: BoolExpr) -> None:
        self.exprs = exprs

    def eval(self, ctx: EvalContext) -> bool:
        return any(e.eval(ctx) for e in self.exprs)


class NotExpr(BoolExpr):
    def __init__(self, expr: BoolExpr) -> None:
        self.expr = expr

    def eval(self, ctx: EvalContext) -> bool:
        return not self.expr.eval(ctx)
