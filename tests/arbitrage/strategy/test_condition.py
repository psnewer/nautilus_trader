"""Condition / EvalResult / evaluate_tree —— 嵌套树求值算法(Q21 核心)。

对应用例:strategy-4.framework.cond.{1-6}
evaluate_tree 是纯函数(无副作用,fire 由顶层),所有用例可全单测。
"""

from src.arbitrage.strategy.bool_expr import AndExpr
from src.arbitrage.strategy.bool_expr import SignalRef
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import Condition
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.condition import EvalResult
from src.arbitrage.strategy.condition import evaluate_tree
from src.arbitrage.strategy.signals import SignalStore


# ── 小工具:计数 Check / Action(stub,绝不应被执行)──
class _RecordingCheck(Check):
    def __init__(self, returns: bool) -> None:
        self._returns = returns
        self.calls = 0
    def passes(self, ctx):
        self.calls += 1
        return self._returns


class _NeverExecutedAction(Action):
    """evaluate 是无副作用的 —— action 不应被本算法直接 await。"""
    def __init__(self) -> None:
        self.calls = 0
    async def execute(self, ctx):
        self.calls += 1


def _ctx(store=None) -> EvalContext:
    return EvalContext(pair_id="match_X", snapshot=None, store=store or SignalStore())


def _leaf(self_true: bool, checks: list[Check] | None = None, action: Action | None = None) -> Condition:
    """叶子 condition 工厂:固定 self_hits 真假 + checktion + actions。"""
    store_key = "x_true" if self_true else "x_false"
    expr = SignalRef(store_key)
    actions = [action] if action else []
    return Condition(self_hits=expr, checktion=checks or [], actions=actions)


def _store_with(key, val=True) -> SignalStore:
    s = SignalStore(); s.set_persistent(key, val); return s


# ── cond.1: self_hits=False → 直接 hit=False ────────────────────
def test_self_hits_false_returns_no_hit():
    """cond.1: self_hits 不通过 → hit=False,即使 checktion/actions 都设了也不跑。"""
    check = _RecordingCheck(returns=True)
    action = _NeverExecutedAction()
    cond = _leaf(self_true=False, checks=[check], action=action)
    res = evaluate_tree(cond, _ctx())                       # store 里没设 x_false → SignalRef False
    assert res == EvalResult(hit=False, pending_actions=[])
    assert check.calls == 0 and action.calls == 0           # 没下沉到 checktion


# ── cond.2: sub_conditions 第一个命中即停 ───────────────────────
def test_sub_conditions_first_hit_returns_immediately():
    """cond.2: sub 第一个命中(互斥)→ 后续 sub 不评估。"""
    s = _store_with("x_true")
    action_first = _NeverExecutedAction()
    action_second = _NeverExecutedAction()
    # 第一个叶子:self_true,checktion 空过,action_first 待 fire
    first = _leaf(self_true=True, action=action_first)
    # 第二个叶子:self_true,如果跑会命中,action_second 待 fire(不应被求值)
    second_check_spy = _RecordingCheck(returns=True)
    second = _leaf(self_true=True, checks=[second_check_spy], action=action_second)
    root = Condition(
        self_hits=SignalRef("x_true"),
        sub_conditions=[first, second],
    )
    res = evaluate_tree(root, _ctx(s))
    assert res.hit and res.pending_actions == [action_first]   # 第一个的 actions
    assert second_check_spy.calls == 0                         # 后续 sub 没跑(互斥)
    assert action_first.calls == 0 and action_second.calls == 0  # evaluate 不 fire


# ── cond.3: sub_conditions 全 miss → hit=False ──────────────────
def test_sub_conditions_all_miss_returns_no_hit():
    """cond.3: 自身命中但子组合全 miss → hit=False(`pending_actions=[]`)。"""
    s = _store_with("x_true")
    miss1 = _leaf(self_true=False)
    miss2 = _leaf(self_true=False)
    root = Condition(self_hits=SignalRef("x_true"), sub_conditions=[miss1, miss2])
    res = evaluate_tree(root, _ctx(s))
    assert res == EvalResult(hit=False, pending_actions=[])


# ── cond.4: 叶子 checktion 全过 → 待 fire actions ────────────────
def test_leaf_checktion_all_pass_returns_pending_actions():
    """cond.4: 叶子节点 checktion(AND)全过 + actions 非空 → (hit=True, pending=actions)。"""
    s = _store_with("x_true")
    action = _NeverExecutedAction()
    cond = _leaf(self_true=True, checks=[_RecordingCheck(True), _RecordingCheck(True)], action=action)
    res = evaluate_tree(cond, _ctx(s))
    assert res == EvalResult(hit=True, pending_actions=[action])
    assert action.calls == 0                                # evaluate 不 fire


# ── cond.5: checktion 空 list → 默认通过 ────────────────────────
def test_leaf_empty_checktion_default_pass():
    """cond.5: checktion 空 list = `all([])=True` 默认通过。"""
    s = _store_with("x_true")
    action = _NeverExecutedAction()
    cond = _leaf(self_true=True, checks=[], action=action)
    res = evaluate_tree(cond, _ctx(s))
    assert res.hit and res.pending_actions == [action]


# ── cond.6: actions=[] → 仍 hit=True,pending_actions=[] ─────
def test_leaf_no_action_still_hits():
    """cond.6: 叶子 actions=[] → hit=True 但 pending=[](上层无事可 fire)。"""
    s = _store_with("x_true")
    cond = _leaf(self_true=True, checks=[], action=None)
    res = evaluate_tree(cond, _ctx(s))
    assert res == EvalResult(hit=True, pending_actions=[])


# ── 补充:叶子 checktion 任一 fail → hit=False ──────────────────
def test_leaf_one_check_fails_returns_no_hit():
    """checktion 任一 fail(AND 全过才算)→ hit=False。"""
    s = _store_with("x_true")
    action = _NeverExecutedAction()
    cond = _leaf(self_true=True, checks=[_RecordingCheck(True), _RecordingCheck(False)], action=action)
    res = evaluate_tree(cond, _ctx(s))
    assert not res.hit and res.pending_actions == []
    assert action.calls == 0


# ── 补充:深嵌套树 ──────────────────────────────────────────────
def test_nested_two_levels_inner_sub_hits():
    """深嵌套:root → sub_inner → leaf,内层命中冒泡到 root。"""
    s = _store_with("x_true")
    action = _NeverExecutedAction()
    leaf = _leaf(self_true=True, action=action)
    inner = Condition(self_hits=SignalRef("x_true"), sub_conditions=[leaf])
    root = Condition(self_hits=SignalRef("x_true"), sub_conditions=[inner])
    res = evaluate_tree(root, _ctx(s))
    assert res.hit and res.pending_actions == [action]


# ── 补充:BoolExpr 求值不消费 transient(关键不变量)─────────
def test_evaluate_does_not_consume_transient_signals():
    """integ:跑一遍 evaluate 后,transient 信号仍可被 Action 内部 get 消费。"""
    s = SignalStore(); s.set_transient("rebate", 0.03)
    expr = SignalRef("rebate", pred=lambda v: v is not None and v > 0)
    cond = Condition(self_hits=expr)
    evaluate_tree(cond, _ctx(s))
    assert s.get("rebate") == 0.03                          # 求值没消费
