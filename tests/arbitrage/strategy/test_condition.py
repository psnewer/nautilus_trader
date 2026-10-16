"""Condition / EvalResult / evaluate_tree —— 嵌套树求值算法。"""

import asyncio

from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import AndCheckExpr
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import Condition
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.condition import EvalResult
from src.arbitrage.strategy.condition import NotCheckExpr
from src.arbitrage.strategy.condition import OrCheckExpr
from src.arbitrage.strategy.condition import evaluate_tree
from src.arbitrage.strategy.condition import evaluate_tree_to_plan
from src.arbitrage.strategy.execution_plan import ExecutionPlan
from src.arbitrage.strategy.execution_plan import PreparedOrder


class _ConstantQuery(StateQuery):
    def __init__(self, value: bool) -> None:
        self.value = value

    def matches(self, ctx: EvalContext) -> bool:
        return self.value


class _RecordingQuery(StateQuery):
    def __init__(self, value: bool, label: str) -> None:
        self.value = value
        self.label = label

    def matches(self, ctx: EvalContext) -> bool:
        ctx.scratch["query_selected"] = self.label
        return self.value


class _RecordingCheck(Check):
    def __init__(self, returns: bool, *, label: str | None = None) -> None:
        self._returns = returns
        self._label = label
        self.calls = 0

    def passes(self, ctx):
        self.calls += 1
        if self._label is not None:
            ctx.scratch["selected"] = self._label
        return self._returns


class _NeverExecutedAction(Action):
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, ctx):
        self.calls += 1


class _NoPlanAction(Action):
    def __init__(self, label: str) -> None:
        self.label = label
        self.calls = 0

    async def execute(self, ctx):
        self.calls += 1
        ctx.scratch["discarded_action"] = self.label


class _PlanAction(Action):
    def __init__(self, *, cancel: bool = False) -> None:
        self.cancel = cancel
        self.calls = 0

    async def execute(self, ctx):
        self.calls += 1
        if self.cancel:
            ctx.scratch["execution_plan"] = ExecutionPlan.cancel_pair(
                ctx.pair_id,
                "test",
                ["O-1"],
            )
            return
        ctx.scratch["execution_plan"] = ExecutionPlan.submit(
            ctx.pair_id,
            [
                PreparedOrder(
                    spec={"instrument_id": "Y.POLYMARKET"},
                    venue="POLYMARKET",
                    role="yes",
                ),
            ],
        )


def _ctx() -> EvalContext:
    return EvalContext(pair_id="match_X")


def _leaf(
    self_true: bool,
    checks: list[Check] | None = None,
    action: Action | None = None,
) -> Condition:
    return Condition(
        self_hits=_ConstantQuery(self_true),
        checktion=AndCheckExpr(*(checks or [])),
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


def test_leaf_checktion_and_all_pass_returns_pending_actions():
    action = _NeverExecutedAction()
    cond = _leaf(True, [_RecordingCheck(True), _RecordingCheck(True)], action)
    assert evaluate_tree(cond, _ctx()) == EvalResult(hit=True, pending_actions=[action])
    assert action.calls == 0


def test_leaf_empty_checktion_and_default_pass():
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


def test_checktion_or_commits_only_first_successful_branch():
    failed = _RecordingCheck(False, label="failed")
    passed = _RecordingCheck(True, label="passed")
    skipped = _RecordingCheck(True, label="skipped")
    ctx = _ctx()
    cond = Condition(
        self_hits=_ConstantQuery(True),
        checktion=OrCheckExpr(failed, passed, skipped),
    )

    assert evaluate_tree(cond, ctx).hit is True
    assert ctx.scratch == {"selected": "passed"}
    assert failed.calls == 1
    assert passed.calls == 1
    assert skipped.calls == 0


def test_checktion_and_failure_rolls_back_prior_success():
    ctx = _ctx()
    ctx.scratch["baseline"] = True
    cond = Condition(
        self_hits=_ConstantQuery(True),
        checktion=AndCheckExpr(
            _RecordingCheck(True, label="passed"),
            _RecordingCheck(False, label="failed"),
        ),
    )

    assert evaluate_tree(cond, ctx).hit is False
    assert ctx.scratch == {"baseline": True}


def test_checktion_not_never_commits_child_scratch():
    ctx = _ctx()
    cond = Condition(
        self_hits=_ConstantQuery(True),
        checktion=NotCheckExpr(_RecordingCheck(False, label="discarded")),
    )

    assert evaluate_tree(cond, ctx).hit is True
    assert ctx.scratch == {}


def test_nested_two_levels_inner_sub_hits():
    action = _NeverExecutedAction()
    leaf = _leaf(True, action=action)
    inner = Condition(self_hits=_ConstantQuery(True), sub_conditions=[leaf])
    root = Condition(self_hits=_ConstantQuery(True), sub_conditions=[inner])
    res = evaluate_tree(root, _ctx())
    assert res.hit and res.pending_actions == [action]


def test_parent_query_output_rolls_back_when_all_children_miss():
    ctx = _ctx()
    ctx.scratch["baseline"] = True
    root = Condition(
        self_hits=_RecordingQuery(True, "parent"),
        sub_conditions=[_leaf(False), _leaf(False)],
    )

    assert evaluate_tree(root, ctx).hit is False
    assert ctx.scratch == {"baseline": True}


def test_runtime_continues_to_sibling_when_actions_produce_no_plan():
    ctx = _ctx()
    ctx.scratch["baseline"] = True
    no_plan = _NoPlanAction("first")
    plan = _PlanAction()
    second_check = _RecordingCheck(True, label="second")
    root = Condition(
        self_hits=_ConstantQuery(True),
        sub_conditions=[
            _leaf(True, [_RecordingCheck(True, label="first")], no_plan),
            _leaf(True, [second_check], plan),
        ],
    )

    result = asyncio.run(evaluate_tree_to_plan(root, ctx))

    assert result.hit is True
    assert result.pending_actions == [plan]
    assert no_plan.calls == 1
    assert plan.calls == 1
    assert second_check.calls == 1
    assert "discarded_action" not in ctx.scratch
    assert ctx.scratch["selected"] == "second"
    assert ctx.scratch["execution_plan"].kind == "submit"


def test_runtime_cancel_plan_is_final_and_stops_siblings():
    ctx = _ctx()
    cancel = _PlanAction(cancel=True)
    skipped_check = _RecordingCheck(True)
    root = Condition(
        self_hits=_ConstantQuery(True),
        sub_conditions=[
            _leaf(True, action=cancel),
            _leaf(True, [skipped_check], _PlanAction()),
        ],
    )

    result = asyncio.run(evaluate_tree_to_plan(root, ctx))

    assert result.hit is True
    assert ctx.scratch["execution_plan"].kind == "cancel_pair"
    assert skipped_check.calls == 0


def test_runtime_all_no_plan_branches_miss_and_restore_scratch():
    ctx = _ctx()
    ctx.scratch["baseline"] = True
    first = _NoPlanAction("first")
    second = _NoPlanAction("second")
    root = Condition(
        self_hits=_RecordingQuery(True, "parent"),
        sub_conditions=[
            _leaf(True, action=first),
            _leaf(True, action=second),
        ],
    )

    result = asyncio.run(evaluate_tree_to_plan(root, ctx))

    assert result == EvalResult(hit=False, pending_actions=[])
    assert first.calls == 1
    assert second.calls == 1
    assert ctx.scratch == {"baseline": True}
