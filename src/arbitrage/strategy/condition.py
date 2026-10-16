"""
Condition / CheckExpr / Check / Action / EvalResult —— condition 嵌套树的核心类型(Q21)。

同步结构求值 `evaluate_tree`：
  1. `self_hits.eval(ctx)` False → 返 EvalResult(hit=False)
  2. sub_conditions 非空 → 递归求值(互斥,命中即停);全没命中返 False
  3. 叶子(sub_conditions 空)→ checktion.eval(ctx) → 返 (hit=True, pending_actions=actions)

同步 `evaluate_tree` 只做结构求值并返回 pending actions，供纯逻辑测试和兼容调用。
生产 `StrategyEvaluator` 使用异步 `evaluate_tree_to_plan`：叶子 checktion 通过后立即串行执行
Action；只有形成最终 `ExecutionPlan` 才真正命中，否则回滚该分支 scratch 并继续兄弟分支。
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field

from src.arbitrage.strategy.bool_expr import BoolExpr
from src.arbitrage.strategy.execution_plan import ExecutionPlan


@dataclass
class EvalContext:
    """求值时的上下文,evaluator 顶层构造,贯穿整轮 evaluate + Action.execute。"""

    pair_id: str
    cache: object | None = None
    pair_registry: object | None = None
    sports_store: object | None = None
    phase_store: object | None = None
    # 活动 pair 的追加式连续触发历史；仅专用 Action 读写，比赛 ended 后统一回收。
    consecutive_trigger_store: object | None = None
    # in_game 部分成交撤单后才按需写入的持仓平仓盘数覆盖；无条目时使用配置默认值。
    in_game_position_store: object | None = None
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
    pair_order_canceler: object | None = None  # Callable[[str, tuple[str, ...]], int] | None
    # ShareLimitModification 等 Action 需要读取持仓数据计算 remaining
    portfolio: object | None = None  # ArbitragePortfolio;运行时类型避循环 import
    # Web Arbitrage 配置提供的运行时默认值;strategy JSON params 显式配置时覆盖这些默认值。
    strategy_defaults: dict = field(default_factory=dict)
    # 本轮评估的触发事件名：OrderBookDeltas / MarketOrderBookDeltas / MatchedPair 等。
    # price_change_recovery 只认 instrument / market 级 OrderBookDeltas。
    event_name: str | None = None
    # 非行情触发树可读取原始触发事件及其终态订单；普通行情评估均为 None。
    trigger_event: object | None = None
    trigger_order: object | None = None
    # 本轮评估的 NT clock 时间，供基于 Order 时间戳的 Check 做可测试比较。
    ts_now_ns: int | None = None
    # 本轮评估所属的树种类:"arbitrage" / "compensation";供 Action 推导默认 intent
    # (补偿树默认 recovery、套利树默认 arbitrage)。None 时按 arbitrage 兜底。
    tree_kind: str | None = None


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
    # 运行时依次执行；空 actions 不能形成最终计划，因此 evaluate_tree_to_plan 不命中。
    actions: list[Action] = field(default_factory=list)


@dataclass
class EvalResult:
    """树求值结果；运行时 hit=True 表示已经形成最终执行计划。"""

    hit: bool
    # 同步结构求值表示待执行 Action；运行时表示形成最终计划的已执行 Action 链。
    pending_actions: list[Action] = field(default_factory=list)


def evaluate_tree(cond: Condition, ctx: EvalContext) -> EvalResult:
    """Q21 主算法:递归求值 condition 嵌套树，生成本树 scratch，但不执行 action。

    与 `StrategyEvaluator` 解耦:运行时 Strategy 只做 orchestration(live state / gather / fire),
    本函数纯逻辑可全单测。

    1. `self_hits.eval(ctx)` False → `EvalResult(hit=False)`
    2. `sub_conditions` 非空 → 递归(互斥,命中即停;全 miss 返 False)
    3. 叶子(`sub_conditions` 空)→ `checktion.eval(ctx)`(空 AND 默认通过)
       → `EvalResult(hit=True, pending_actions=actions)`(空 actions 时上层不执行)
    """
    before = deepcopy(ctx.scratch)
    try:
        if not cond.self_hits.eval(ctx):
            _restore_scratch(ctx, before)
            return EvalResult(hit=False)

        if cond.sub_conditions:
            for sub in cond.sub_conditions:
                res = evaluate_tree(sub, ctx)
                if res.hit:
                    return res                        # 互斥:命中即停
            _restore_scratch(ctx, before)
            return EvalResult(hit=False)

        if cond.checktion.eval(ctx):
            return EvalResult(hit=True, pending_actions=cond.actions)
        _restore_scratch(ctx, before)
        return EvalResult(hit=False)
    except Exception:
        _restore_scratch(ctx, before)
        raise


async def evaluate_tree_to_plan(cond: Condition, ctx: EvalContext) -> EvalResult:
    """运行时求值树；Action 形成最终执行计划后才算命中。

    checktion 通过但完整 Action 链未形成 submit/cancel 计划时，本候选分支 scratch 回滚，
    随后继续求值下一个兄弟分支。Action 对专用运行时 Store 的显式写入不属于 scratch
    事务，例如连续触发 observation 仍按其自身契约保留。
    """
    before = deepcopy(ctx.scratch)
    try:
        if not cond.self_hits.eval(ctx):
            _restore_scratch(ctx, before)
            return EvalResult(hit=False)

        if cond.sub_conditions:
            for sub in cond.sub_conditions:
                result = await evaluate_tree_to_plan(sub, ctx)
                if result.hit:
                    return result
            _restore_scratch(ctx, before)
            return EvalResult(hit=False)

        if not cond.checktion.eval(ctx):
            _restore_scratch(ctx, before)
            return EvalResult(hit=False)

        for action in cond.actions:
            await action.execute(ctx)
        if _has_final_plan(ctx):
            return EvalResult(hit=True, pending_actions=cond.actions)

        _restore_scratch(ctx, before)
        return EvalResult(hit=False)
    except Exception:
        _restore_scratch(ctx, before)
        raise


def _has_final_plan(ctx: EvalContext) -> bool:
    plan = ctx.scratch.get("execution_plan")
    if not isinstance(plan, ExecutionPlan):
        return False
    if plan.kind == "submit":
        return bool(plan.orders)
    return plan.kind == "cancel_pair"
