"""Condition / EvalResult / evaluate_tree —— 嵌套树求值算法。"""

from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import Condition
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.condition import EvalResult
from src.arbitrage.strategy.condition import evaluate_tree


class _ConstantQuery(StateQuery):
    def __init__(self, value: bool) -> None:
        self.value = value

    def matches(self, ctx: EvalContext) -> bool:
        return self.value


class _RecordingCheck(Check):
    def __init__(self, returns: bool) -> None:
        self._returns = returns
        self.calls = 0

    def passes(self, ctx):
        self.calls += 1
        return self._returns


class _NeverExecutedAction(Action):
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, ctx):
        self.calls += 1


def _ctx() -> EvalContext:
    return EvalContext(pair_id="match_X")


def _leaf(
    self_true: bool,
    checks: list[Check] | None = None,
    action: Action | None = None,
) -> Condition:
    return Condition(
        self_hits=_ConstantQuery(self_true),
        checktion=checks or [],
        actions=[action] if action else [],
    )


def test_self_hits_false_returns_no_hit():
    check = _RecordingCheck(returns=True)
    action = _NeverExecutedAction()
    res = evaluate_tree(_leaf(False, [check], action), _ctx())
    assert res == EvalResult(hit=False, pending_actions=[])
    assert check.calls == 0
    assert action.calls == 0


def test_sub_conditions_first_hit_returns_immediately():
    action_first = _NeverExecutedAction()
    action_second = _NeverExecutedAction()
    second_check = _RecordingCheck(returns=True)
    root = Condition(
        self_hits=_ConstantQuery(True),
        sub_conditions=[
            _leaf(True, action=action_first),
            _leaf(True, checks=[second_check], action=action_second),
        ],
    )
    res = evaluate_tree(root, _ctx())
    assert res.hit and res.pending_actions == [action_first]
    assert second_check.calls == 0
    assert action_first.calls == 0
    assert action_second.calls == 0


def test_sub_conditions_all_miss_returns_no_hit():
    root = Condition(
        self_hits=_ConstantQuery(True),
        sub_conditions=[_leaf(False), _leaf(False)],
    )
    assert evaluate_tree(root, _ctx()) == EvalResult(hit=False, pending_actions=[])


def test_leaf_checktion_all_pass_returns_pending_actions():
    action = _NeverExecutedAction()
    cond = _leaf(True, [_RecordingCheck(True), _RecordingCheck(True)], action)
    assert evaluate_tree(cond, _ctx()) == EvalResult(hit=True, pending_actions=[action])
    assert action.calls == 0


def test_leaf_empty_checktion_default_pass():
    action = _NeverExecutedAction()
    res = evaluate_tree(_leaf(True, action=action), _ctx())
    assert res.hit and res.pending_actions == [action]


def test_leaf_no_action_still_hits():
    assert evaluate_tree(_leaf(True), _ctx()) == EvalResult(hit=True, pending_actions=[])


def test_leaf_one_check_fails_returns_no_hit():
    action = _NeverExecutedAction()
    cond = _leaf(True, [_RecordingCheck(True), _RecordingCheck(False)], action)
    assert evaluate_tree(cond, _ctx()) == EvalResult(hit=False, pending_actions=[])
    assert action.calls == 0


def test_nested_two_levels_inner_sub_hits():
    action = _NeverExecutedAction()
    leaf = _leaf(True, action=action)
    inner = Condition(self_hits=_ConstantQuery(True), sub_conditions=[leaf])
    root = Condition(self_hits=_ConstantQuery(True), sub_conditions=[inner])
    res = evaluate_tree(root, _ctx())
    assert res.hit and res.pending_actions == [action]
