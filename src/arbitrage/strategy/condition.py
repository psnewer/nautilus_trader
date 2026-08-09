"""
Condition / CheckExpr / Check / Action / EvalResult —— condition 嵌套树的核心类型(Q21)。

evaluate 流程(`StrategyEvaluator._evaluate_tree`):
  1. `self_hits.eval(ctx)` False → 返 EvalResult(hit=False)
  2. sub_conditions 非空 → 递归求值(互斥,命中即停);全没命中返 False
  3. 叶子(sub_conditions 空)→ checktion.eval(ctx) → 返 (hit=True, pending_actions=actions)

**evaluate 不执行 Action**:返 `EvalResult { hit, pending_actions }`。顶层让套利/补偿 Action
链在各自上下文内生成执行计划，再统一选择和分发。Check 可向本树独占的 `ctx.scratch`
写派生结果；actions 依次串行执行。
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field

from src.arbitrage.strategy.bool_expr import BoolExpr


@dataclass
class EvalContext:
    """求值时的上下文,evaluator 顶层构造,贯穿整轮 evaluate + Action.execute。"""

    pair_id: str
    cache: object | None = None
    pair_registry: object | None = None
    sports_store: object | None = None
    # 评估开始时该 pair 的 position 摘要；所有腿透传给 Execution barrier release 前比较。
    # (#317:open_orders_digest 已删 —— barrier 不再做 open-order 校验,承 #316 per-pair ≤1。)
    positions_digest: str | None = None
    # slice 9(#49):per-eval scratch — Check 算 derived 数据(如 legs)给同 condition 树的 Action 用;
    # per-eval 自动隔离(每次 evaluate 新建 ctx),无 race。只在同 condition 树内 Check→Action 传。
    scratch: dict = field(default_factory=dict)
    # Evaluator 统一分发执行计划时使用；树内 Action 不得直接调用。
    # `spec` schema:{instrument_id, side: "BUY"|"SELL", qty: float, price: float}
    submitter: object | None = None  # Callable[[dict], Awaitable[None]] | None;运行时类型避循环 import
    # Evaluator 分发 cancel_pair 计划时调用；实现必须走 NT 原生 CancelOrder。
    pair_order_canceler: object | None = None  # Callable[[str], int] | None
    # ShareLimitModification 等 Action 需要读取持仓数据计算 remaining
    portfolio: object | None = None  # ArbitragePortfolio;运行时类型避循环 import
    # Web Arbitrage 配置提供的运行时默认值;strategy JSON params 显式配置时覆盖这些默认值。
    strategy_defaults: dict = field(default_factory=dict)
    # #trend:StrategyEvaluator 每帧在 on_order_book_deltas 更新的价格趋势视图,
    # key = str(instrument_id)(= venue + 该腿/outcome),value = 最近一帧 Δprob(概率空间,正=概率变大)。
    # Check 按 (pair, outcome, venue) 反查 instrument_id 读取;None = 未接入趋势。
    price_trend: dict | None = None


class CheckExpr(ABC):
    """带 `scratch` 事务语义的决策核查表达式。"""

    @abstractmethod
    def eval(self, ctx: EvalContext) -> bool:
        """按配置顺序同步求值，并只提交命中分支的 `scratch`。"""


class Check(CheckExpr):
    """决策核查叶子；失败或异常时回滚本叶子产生的 `scratch`。"""

    @abstractmethod
    def passes(self, ctx: EvalContext) -> bool:
        """返回本核查是否通过。"""

    def eval(self, ctx: EvalContext) -> bool:
        before = deepcopy(ctx.scratch)
        try:
            passed = bool(self.passes(ctx))
        except Exception:
            _restore_scratch(ctx, before)
            raise
        if not passed:
            _restore_scratch(ctx, before)
        return passed


class AndCheckExpr(CheckExpr):
    """全部命中才命中；按顺序短路，空 AND 默认命中。"""

    def __init__(self, *exprs: CheckExpr) -> None:
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


class OrCheckExpr(CheckExpr):
    """首个命中分支即命中；失败分支不留下 `scratch`，空 OR 默认不命中。"""

    def __init__(self, *exprs: CheckExpr) -> None:
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


class NotCheckExpr(CheckExpr):
    """反转子表达式结果；被否定表达式的 `scratch` 永不提交。"""

    def __init__(self, expr: CheckExpr) -> None:
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


class Action(ABC):
    """命中后执行(Q21)；Evaluator 在自己的评估 task 内按顺序 await。"""

    @abstractmethod
    async def execute(self, ctx: EvalContext) -> None:
        ...


@dataclass
class Condition:
    """condition 嵌套树节点。

    评估顺序:`self_hits` → `sub_conditions`(若非空,互斥)/ 叶子的 `checktion` 表达式→ `actions`。
    """

    self_hits: BoolExpr
    sub_conditions: list[Condition] = field(default_factory=list)
    checktion: CheckExpr = field(default_factory=AndCheckExpr)  # 空 AND = 默认通过
    actions: list[Action] = field(default_factory=list)       # 依次执行; 空 list = no-op(仍 hit=True)


@dataclass
class EvalResult:
    """evaluate 返回(无副作用,fire 由顶层做)。"""

    hit: bool
    pending_actions: list[Action] = field(default_factory=list)  # hit=True 且非 sub_conditions 路径时才非空


def evaluate_tree(cond: Condition, ctx: EvalContext) -> EvalResult:
    """Q21 主算法:递归求值 condition 嵌套树，生成本树 scratch，但不执行 action。

    与 `StrategyEvaluator` 解耦:运行时 Strategy 只做 orchestration(live state / gather / fire),
    本函数纯逻辑可全单测。

    1. `self_hits.eval(ctx)` False → `EvalResult(hit=False)`
    2. `sub_conditions` 非空 → 递归(互斥,命中即停;全 miss 返 False)
    3. 叶子(`sub_conditions` 空)→ `checktion.eval(ctx)`(空 AND 默认通过)
       → `EvalResult(hit=True, pending_actions=actions)`(空 actions 时上层不执行)
    """
    if not cond.self_hits.eval(ctx):
        return EvalResult(hit=False)

    if cond.sub_conditions:
        for sub in cond.sub_conditions:
            res = evaluate_tree(sub, ctx)
            if res.hit:
                return res                            # 互斥:命中即停
        return EvalResult(hit=False)

    if cond.checktion.eval(ctx):
        return EvalResult(hit=True, pending_actions=cond.actions)
    return EvalResult(hit=False)
