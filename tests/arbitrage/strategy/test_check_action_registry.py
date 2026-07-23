"""Slice 5(part 1):Check/Action 类型 registry + build。"""

import pytest

from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.check_action_registry import StrategyConfigError
from src.arbitrage.strategy.check_action_registry import _reset_for_tests
from src.arbitrage.strategy.check_action_registry import build_action
from src.arbitrage.strategy.check_action_registry import build_check
from src.arbitrage.strategy.check_action_registry import build_state_query
from src.arbitrage.strategy.check_action_registry import register_action
from src.arbitrage.strategy.check_action_registry import register_check
from src.arbitrage.strategy.check_action_registry import register_state_query
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import Check


@pytest.fixture(autouse=True)
def _clean_registry():
    _reset_for_tests()
    yield
    _reset_for_tests()


class _DummyCheck(Check):
    def __init__(self, threshold: float = 0.0) -> None:
        self.threshold = threshold

    def passes(self, ctx) -> bool:
        return True


class _DummyAction(Action):
    def __init__(self, label: str = "x") -> None:
        self.label = label

    async def execute(self, ctx) -> None:
        return None


class _DummyStateQuery(StateQuery):
    def __init__(self, expected_pair_id: str = "") -> None:
        self.expected_pair_id = expected_pair_id

    def matches(self, ctx) -> bool:
        return ctx.pair_id == self.expected_pair_id


# ── register / build basics ──────────────────────────────────────

def test_register_and_build_check():
    register_check("dummy", _DummyCheck)
    inst = build_check({"type": "dummy", "params": {"threshold": 0.5}})
    assert isinstance(inst, _DummyCheck)
    assert inst.threshold == 0.5


def test_register_and_build_action():
    register_action("dummy", _DummyAction)
    inst = build_action({"type": "dummy", "params": {"label": "yo"}})
    assert isinstance(inst, _DummyAction)
    assert inst.label == "yo"


def test_register_and_build_state_query():
    register_state_query("dummy", _DummyStateQuery)
    inst = build_state_query({"type": "dummy", "params": {"expected_pair_id": "pair_X"}})
    assert isinstance(inst, _DummyStateQuery)
    assert inst.expected_pair_id == "pair_X"


def test_build_check_with_no_params():
    register_check("dummy", _DummyCheck)
    inst = build_check({"type": "dummy"})
    assert inst.threshold == 0.0


# ── error paths ──────────────────────────────────────────────────

def test_unknown_check_type_raises():
    with pytest.raises(StrategyConfigError, match="unknown check type"):
        build_check({"type": "nope"})


def test_unknown_action_type_raises():
    with pytest.raises(StrategyConfigError, match="unknown action type"):
        build_action({"type": "nope"})


def test_unknown_state_query_type_raises():
    with pytest.raises(StrategyConfigError, match="unknown state query type"):
        build_state_query({"type": "nope"})


def test_invalid_action_params_raise_strategy_config_error():
    register_action("dummy", _DummyAction)
    with pytest.raises(StrategyConfigError, match="invalid params for action type 'dummy'"):
        build_action({"type": "dummy", "params": {"missing": "x"}})


def test_invalid_check_params_raise_strategy_config_error():
    register_check("dummy", _DummyCheck)
    with pytest.raises(StrategyConfigError, match="invalid params for check type 'dummy'"):
        build_check({"type": "dummy", "params": {"missing": "x"}})


def test_spec_missing_type_raises():
    with pytest.raises(StrategyConfigError, match="must have 'type'"):
        build_check({"params": {}})


def test_double_register_same_class_idempotent():
    register_check("dummy", _DummyCheck)
    register_check("dummy", _DummyCheck)  # 幂等
    assert build_check({"type": "dummy"}).threshold == 0.0


def test_double_register_different_class_raises():
    class _Other(Check):
        def passes(self, ctx): return False

    register_check("dummy", _DummyCheck)
    with pytest.raises(StrategyConfigError, match="already registered"):
        register_check("dummy", _Other)
