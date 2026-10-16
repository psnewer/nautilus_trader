"""
`Condition.self_hits` 的当前状态查询 DSL。

支持 AND/OR/NOT 嵌套;叶子读取 `EvalContext`,命中时可提交本轮派生结果到 scratch。
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from copy import deepcopy
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from src.arbitrage.strategy.condition import EvalContext


class BoolExpr(ABC):
    """Q21 self_hits 的布尔表达式基类。"""

    @abstractmethod
    def eval(self, ctx: EvalContext) -> bool:
        """基于本轮上下文求值。"""


class StateQuery(BoolExpr):
    """self_hits 叶子:查询当前上下文;命中时可向本树 scratch 提交派生结果。"""

    @abstractmethod
    def matches(self, ctx: EvalContext) -> bool:
        """返回当前状态是否满足条件。"""

    def eval(self, ctx: EvalContext) -> bool:
        before = deepcopy(ctx.scratch)
        try:
            matched = bool(self.matches(ctx))
        except Exception:
            _restore_scratch(ctx, before)
            raise
        if not matched:
            _restore_scratch(ctx, before)
        return matched


class AndExpr(BoolExpr):
    """全 True 才 True;空(无子)→ 默认 True(空 AND = vacuous truth,跟 `all([])` 一致)。"""

    def __init__(self, *exprs: BoolExpr) -> None:
        self.exprs = exprs

    def eval(self, ctx: EvalContext) -> bool:
        before = deepcopy(ctx.scratch)
        try:
            for expr in self.exprs:
                if not expr.eval(ctx):
                    _restore_scratch(ctx, before)
                    return False
        except Exception:
            _restore_scratch(ctx, before)
            raise
        return True


class OrExpr(BoolExpr):
    """任一 True 即 True;空 → 默认 False(跟 `any([])` 一致)。"""

    def __init__(self, *exprs: BoolExpr) -> None:
        self.exprs = exprs

    def eval(self, ctx: EvalContext) -> bool:
        before = deepcopy(ctx.scratch)
        for expr in self.exprs:
            _restore_scratch(ctx, before)
            try:
                if expr.eval(ctx):
                    return True
            except Exception:
                _restore_scratch(ctx, before)
                raise
        _restore_scratch(ctx, before)
        return False


class NotExpr(BoolExpr):
    def __init__(self, expr: BoolExpr) -> None:
        self.expr = expr

    def eval(self, ctx: EvalContext) -> bool:
        before = deepcopy(ctx.scratch)
        try:
            return not self.expr.eval(ctx)
        finally:
            _restore_scratch(ctx, before)


def _restore_scratch(ctx: EvalContext, snapshot: dict) -> None:
    ctx.scratch.clear()
    ctx.scratch.update(snapshot)
