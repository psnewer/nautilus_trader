"""Slice 5(part 2):BoolExpr / Condition / Strategy JSON 递归解析 + Registry 装载。"""

import msgspec
import pytest

from src.arbitrage.config.dispatcher import to_strategy_registry
from src.arbitrage.config.schema import ArbConfig
from src.arbitrage.strategy.bool_expr import AndExpr
from src.arbitrage.strategy.bool_expr import NotExpr
from src.arbitrage.strategy.bool_expr import OrExpr
from src.arbitrage.strategy.bool_expr import SignalRef
from src.arbitrage.strategy.check_action_registry import StrategyConfigError
from src.arbitrage.strategy.check_action_registry import _reset_for_tests
from src.arbitrage.strategy.check_action_registry import register_action
from src.arbitrage.strategy.check_action_registry import register_check
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import Condition
from src.arbitrage.strategy.condition import evaluate_tree
from src.arbitrage.strategy.json_loader import bool_expr_from_json
from src.arbitrage.strategy.json_loader import build_strategy_registry
from src.arbitrage.strategy.json_loader import condition_from_json
from src.arbitrage.strategy.json_loader import strategy_from_json
from src.arbitrage.strategy.signals import SignalStore


class _PassCheck(Check):
    def __init__(self, label: str = "p") -> None: self.label = label
    def passes(self, ctx) -> bool: return True


class _FailCheck(Check):
    def passes(self, ctx) -> bool: return False


class _NoopAction(Action):
    def __init__(self, label: str = "x") -> None: self.label = label
    async def execute(self, ctx) -> None: return None


@pytest.fixture(autouse=True)
def _registry():
    _reset_for_tests()
    register_check("pass", _PassCheck)
    register_check("fail", _FailCheck)
    register_action("noop", _NoopAction)
    yield
    _reset_for_tests()


# ─── bool_expr_from_json ──────────────────────────────────────────

def test_signal_leaf():
    e = bool_expr_from_json({"signal": "live"})
    assert isinstance(e, SignalRef)
    assert e.key == "live"


def test_and_expression():
    e = bool_expr_from_json({"AND": [{"signal": "a"}, {"signal": "b"}]})
    assert isinstance(e, AndExpr)
    assert len(e.exprs) == 2


def test_or_expression():
    e = bool_expr_from_json({"OR": [{"signal": "a"}, {"signal": "b"}]})
    assert isinstance(e, OrExpr)


def test_not_expression():
    e = bool_expr_from_json({"NOT": {"signal": "live"}})
    assert isinstance(e, NotExpr)
    assert isinstance(e.expr, SignalRef)


def test_nested_recursive():
    """AND([live, OR([home, NOT draw])])"""
    e = bool_expr_from_json({"AND": [
        {"signal": "live"},
        {"OR": [{"signal": "home"}, {"NOT": {"signal": "draw"}}]},
    ]})
    assert isinstance(e, AndExpr)
    assert isinstance(e.exprs[1], OrExpr)
    assert isinstance(e.exprs[1].exprs[1], NotExpr)


def test_none_defaults_to_and_empty_true():
    e = bool_expr_from_json(None)
    assert isinstance(e, AndExpr) and len(e.exprs) == 0
    # 空 AND = vacuous truth
    assert e.eval(SignalStore()) is True


def test_multiple_keys_raises():
    with pytest.raises(StrategyConfigError, match="exactly one"):
        bool_expr_from_json({"AND": [], "OR": []})


def test_unknown_key_raises():
    with pytest.raises(StrategyConfigError, match="unknown bool_expr key"):
        bool_expr_from_json({"XOR": []})


def test_signal_value_must_be_str():
    with pytest.raises(StrategyConfigError, match="must be str"):
        bool_expr_from_json({"signal": 123})


# ─── condition_from_json ──────────────────────────────────────────

def test_empty_condition_defaults_to_truthy_pass():
    """全空 spec → self_hits=AndExpr() True / 空 checktion 默认 hit=True / actions=[]。"""
    c = condition_from_json({})
    store = SignalStore()
    res = evaluate_tree(c, _ctx(store))
    assert res.hit is True
    assert res.pending_actions == []


def test_self_hits_false_blocks_evaluation():
    c = condition_from_json({
        "self_hits": {"signal": "missing_signal"},
        "checktion": [{"type": "pass"}],
    })
    store = SignalStore()  # missing_signal 未 set
    res = evaluate_tree(c, _ctx(store))
    assert res.hit is False


def test_self_hits_true_with_passing_check_fires_action():
    c = condition_from_json({
        "self_hits": {"signal": "live"},
        "checktion": [{"type": "pass"}],
        "action": {"type": "noop", "params": {"label": "fire"}},  # 兼容旧格式
    })
    store = SignalStore()
    store.set_persistent("live", True)
    res = evaluate_tree(c, _ctx(store))
    assert res.hit is True
    assert len(res.pending_actions) == 1
    assert isinstance(res.pending_actions[0], _NoopAction)
    assert res.pending_actions[0].label == "fire"


def test_failing_check_short_circuits():
    c = condition_from_json({
        "checktion": [{"type": "pass"}, {"type": "fail"}],
        "action": {"type": "noop"},
    })
    res = evaluate_tree(c, _ctx(SignalStore()))
    assert res.hit is False


def test_recursive_sub_conditions():
    """根 self_hits=True → sub_conditions 互斥;第一个 sub 命中则停。"""
    c = condition_from_json({
        "sub_conditions": [
            {"self_hits": {"signal": "branch_a"}, "action": {"type": "noop", "params": {"label": "A"}}},
            {"self_hits": {"signal": "branch_b"}, "action": {"type": "noop", "params": {"label": "B"}}},
        ],
    })
    store = SignalStore()
    store.set_persistent("branch_b", True)
    res = evaluate_tree(c, _ctx(store))
    assert res.hit and res.pending_actions[0].label == "B"


def test_sub_conditions_all_miss():
    c = condition_from_json({
        "sub_conditions": [{"self_hits": {"signal": "a"}}, {"self_hits": {"signal": "b"}}],
    })
    res = evaluate_tree(c, _ctx(SignalStore()))
    assert res.hit is False


def test_unknown_check_type_in_condition_raises():
    with pytest.raises(StrategyConfigError, match="unknown check type"):
        condition_from_json({"checktion": [{"type": "nope"}]})


def test_unknown_action_type_in_condition_raises():
    with pytest.raises(StrategyConfigError, match="unknown action type"):
        condition_from_json({"action": {"type": "nope"}})


def test_actions_array_format():
    """新 actions 数组格式:多个 action 依次执行。"""
    c = condition_from_json({
        "self_hits": {"signal": "live"},
        "actions": [
            {"type": "noop", "params": {"label": "first"}},
            {"type": "noop", "params": {"label": "second"}},
        ],
    })
    store = SignalStore()
    store.set_persistent("live", True)
    res = evaluate_tree(c, _ctx(store))
    assert res.hit is True
    assert len(res.pending_actions) == 2
    assert res.pending_actions[0].label == "first"
    assert res.pending_actions[1].label == "second"


# ─── strategy_from_json ───────────────────────────────────────────

def test_strategy_with_both_trees():
    spec = {
        "arbitrage_tree": {"checktion": [{"type": "pass"}], "action": {"type": "noop"}},
        "compensation_tree": {"checktion": [{"type": "fail"}]},
    }
    s = strategy_from_json("s1", spec, scope_key="sport:Tennis")
    assert s.scope_key == "sport:Tennis"
    assert s.metadata["id"] == "s1"
    # 套利树 hit
    res = evaluate_tree(s.arbitrage_tree, _ctx(SignalStore()))
    assert res.hit is True
    # 补救树 fail
    res2 = evaluate_tree(s.compensation_tree, _ctx(SignalStore()))
    assert res2.hit is False


def test_strategy_missing_compensation_uses_noop_never_hit():
    """compensation_tree 缺 → 永 False(OrExpr() 空 OR)。"""
    spec = {"arbitrage_tree": {"checktion": []}}
    s = strategy_from_json("s2", spec, scope_key="sport:Soccer")
    res = evaluate_tree(s.compensation_tree, _ctx(SignalStore()))
    assert res.hit is False


def test_strategy_missing_arbitrage_raises():
    with pytest.raises(StrategyConfigError, match="missing arbitrage_tree"):
        strategy_from_json("s3", {}, scope_key="sport:Tennis")


# ─── build_strategy_registry from ArbConfig ────────────────────────

def _cfg(strategies, bindings):
    return msgspec.convert(
        {"strategy": {"strategies": strategies, "bindings": bindings}},
        type=ArbConfig,
    )


def test_registry_binds_sport_competition_pair():
    cfg = _cfg(
        strategies={
            "s_sport": {"arbitrage_tree": {"action": {"type": "noop", "params": {"label": "sport"}}}},
            "s_comp":  {"arbitrage_tree": {"action": {"type": "noop", "params": {"label": "comp"}}}},
            "s_pair":  {"arbitrage_tree": {"action": {"type": "noop", "params": {"label": "pair"}}}},
        },
        bindings=[
            {"scope": "sport:Tennis",          "strategy_id": "s_sport"},
            {"scope": "competition:ATP",       "strategy_id": "s_comp"},
            {"scope": "pair:Tennis|ATP|x|y",   "strategy_id": "s_pair"},
        ],
    )
    reg = to_strategy_registry(cfg)
    # pair 优先,挂载锁定
    assert reg.get_for("Tennis|ATP|x|y", "ATP", "Tennis").metadata["id"] == "s_pair"
    # 退到 competition
    assert reg.get_for("other_pair", "ATP", "Tennis").metadata["id"] == "s_comp"
    # 退到 sport
    assert reg.get_for("other_pair", "OtherComp", "Tennis").metadata["id"] == "s_sport"
    # 全无挂载 → None
    assert reg.get_for("p", "c", "Basketball") is None


def test_registry_pair_id_alias_kind():
    """scope `pair_id:X` 等同 `pair:X`(用户习惯兼容)。"""
    cfg = _cfg(
        strategies={"s": {"arbitrage_tree": {"action": {"type": "noop"}}}},
        bindings=[{"scope": "pair_id:abc", "strategy_id": "s"}],
    )
    reg = to_strategy_registry(cfg)
    assert reg.get_for("abc", None, None) is not None


def test_registry_unknown_strategy_id_raises():
    cfg = _cfg(
        strategies={},
        bindings=[{"scope": "sport:Tennis", "strategy_id": "nope"}],
    )
    with pytest.raises(StrategyConfigError, match="unknown strategy_id"):
        to_strategy_registry(cfg)


def test_registry_bad_scope_kind_raises():
    cfg = _cfg(
        strategies={"s": {"arbitrage_tree": {}}},
        bindings=[{"scope": "league:NHL", "strategy_id": "s"}],
    )
    with pytest.raises(StrategyConfigError, match="kind must be pair/competition/sport"):
        to_strategy_registry(cfg)


def test_registry_bad_scope_format_raises():
    cfg = _cfg(
        strategies={"s": {"arbitrage_tree": {}}},
        bindings=[{"scope": "no_colon", "strategy_id": "s"}],
    )
    with pytest.raises(StrategyConfigError, match="kind:<name>|<kind>:<name>"):
        to_strategy_registry(cfg)


def test_empty_bindings_returns_empty_registry():
    """空 bindings → 空 registry,launcher 仍能起;StrategyEvaluator no-op evaluate。"""
    cfg = _cfg(strategies={}, bindings=[])
    reg = to_strategy_registry(cfg)
    assert reg.get_for("x", "y", "z") is None


def test_strategy_disabled_returns_empty_registry_even_with_bindings():
    """strategy.enabled=false → 保留 OBD 订阅桥,但 registry 为空,不触发策略 Action。"""
    cfg = msgspec.convert(
        {"strategy": {
            "enabled": False,
            "strategies": {"s": {"arbitrage_tree": {"action": {"type": "noop"}}}},
            "bindings": [{"scope": "sport:Tennis", "strategy_id": "s"}],
        }},
        type=ArbConfig,
    )
    reg = to_strategy_registry(cfg)
    assert reg.get_for("any", "ATP", "Tennis") is None


# ─── helpers ──────────────────────────────────────────────────────

def _ctx(store):
    from src.arbitrage.strategy.condition import EvalContext
    return EvalContext(pair_id="test", snapshot=None, store=store)
