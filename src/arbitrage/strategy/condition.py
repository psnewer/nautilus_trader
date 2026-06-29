"""
Condition / Check / Action / EvalResult —— condition 嵌套树的核心类型(Q21)。

evaluate 流程(`StrategyEvaluator._evaluate_tree`):
  1. `self_hits.eval(store)` False → 返 EvalResult(hit=False)
  2. sub_conditions 非空 → 递归求值(互斥,命中即停);全没命中返 False
  3. 叶子(sub_conditions 空)→ all(checktion).passes → 返 (hit=True, pending_actions=actions)

**evaluate 无副作用**:返 `EvalResult { hit, pending_actions }`,**fire 由顶层做**(让套利/补救
并行 evaluate 后,套利结果决定补救是否 fire)。actions 依次串行执行(如 ShareLimitModification → PlaceBetsAction)。
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from dataclasses import field

from src.arbitrage.strategy.bool_expr import BoolExpr


@dataclass
class EvalContext:
    """求值时的上下文,evaluator 顶层构造,贯穿整轮 evaluate + Action.execute。"""

    pair_id: str
    snapshot: object | None = None     # OpportunitySnapshot(Q20),由 Slice D 定义;此处只占位
    store: object | None = None        # SignalStore(双状态信号);evaluator 装时是 .view(pair_id) per-pair 视图
    # slice 9(#49):per-eval scratch — Check 算 derived 数据(如 legs)给同 condition 树的 Action 用;
    # per-eval 自动隔离(每次 evaluate 新建 ctx),无 race。**只在同 condition 树内 Check→Action 传**,
    # 跨 evaluate 持久应该走 store(SignalStore.persistent)。
    scratch: dict = field(default_factory=dict)
    # slice 10a(#50):Action 真出单 callable —— evaluator 构造时注入,Action 经
    # `await ctx.submitter(spec)` 提交;`None` 时 Action 应 log-only fallback。
    # `spec` schema:{instrument_id, side: "BUY"|"SELL", qty: float, price: float}
    submitter: object | None = None  # Callable[[dict], Awaitable[None]] | None;运行时类型避循环 import
    # ShareLimitModification 等 Action 需要读取持仓数据计算 remaining
    portfolio: object | None = None  # ArbitragePortfolio;运行时类型避循环 import


class Check(ABC):
    """决策核查(Q21):checktion 是 list[Check],AND 关系,全过才算通过。"""

    @abstractmethod
    def passes(self, ctx: EvalContext) -> bool:
        """返 True 通过;空 checktion list 默认通过(`all([])==True`)。"""


class Action(ABC):
    """命中后执行(Q21):**fire-and-forget**(evaluator 顶层 `create_task(action.execute)`)。

    `execute` 可以是 async(下单 IO);返回值忽略(evaluate 不等其完成)。
    """

    @abstractmethod
    async def execute(self, ctx: EvalContext) -> None:
        ...


@dataclass
class Condition:
    """condition 嵌套树节点。

    评估顺序:`self_hits` → `sub_conditions`(若非空,互斥)/ 叶子的 `checktion`(AND)→ `actions`。
    """

    self_hits: BoolExpr
    sub_conditions: list["Condition"] = field(default_factory=list)
    checktion: list[Check] = field(default_factory=list)     # 空 list = 默认通过
    actions: list[Action] = field(default_factory=list)       # 依次执行; 空 list = no-op(仍 hit=True)


@dataclass
class EvalResult:
    """evaluate 返回(无副作用,fire 由顶层做)。"""

    hit: bool
    pending_actions: list[Action] = field(default_factory=list)  # hit=True 且非 sub_conditions 路径时才非空


def evaluate_tree(cond: Condition, ctx: EvalContext) -> EvalResult:
    """Q21 主算法:递归求值 condition 嵌套树,**无副作用**(不执行 action)。

    与 `StrategyEvaluator` 解耦:Actor 只做 orchestration(snapshot / gather / fire),
    本函数纯逻辑可全单测。

    1. `self_hits.eval(store)` False → `EvalResult(hit=False)`
    2. `sub_conditions` 非空 → 递归(互斥,命中即停;全 miss 返 False)
    3. 叶子(`sub_conditions` 空)→ `all(check.passes for check in checktion)`(空 list 默认通过)
       → `EvalResult(hit=True, pending_action=action)`(action=None 时上层 fire 跳过)
    """
    if not cond.self_hits.eval(ctx.store):
        return EvalResult(hit=False)

    if cond.sub_conditions:
        for sub in cond.sub_conditions:
            res = evaluate_tree(sub, ctx)
            if res.hit:
                return res                            # 互斥:命中即停
        return EvalResult(hit=False)

    if all(check.passes(ctx) for check in cond.checktion):
        return EvalResult(hit=True, pending_actions=cond.actions)
    return EvalResult(hit=False)
