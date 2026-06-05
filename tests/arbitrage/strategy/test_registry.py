"""StrategyRegistry —— scope 优先级 + 挂载存在锁定(Q21-a)。

对应用例:strategy-4.framework.reg.{1-4}
"""

import pytest

from src.arbitrage.strategy.bool_expr import SignalRef
from src.arbitrage.strategy.condition import Condition
from src.arbitrage.strategy.registry import Strategy
from src.arbitrage.strategy.registry import StrategyRegistry


def _stub_strategy(scope_key: str) -> Strategy:
    """空叶子条件树占位(测的是 registry 查找,不是 evaluate)。"""
    leaf = Condition(self_hits=SignalRef("noop"))
    return Strategy(scope_key=scope_key, arbitrage_tree=leaf, compensation_tree=leaf)


# ── reg.1: 只挂 sport,该 sport 下任意 pair 都返该策略 ───────────
def test_only_sport_mounted_returns_sport_strategy_for_any_pair():
    r = StrategyRegistry()
    sport_s = _stub_strategy("sport:Soccer")
    r.register_sport("Soccer", sport_s)
    assert r.get_for("any_pair", "any_comp", "Soccer") is sport_s
    assert r.get_for("other_pair", "other_comp", "Soccer") is sport_s
    # 不同 sport → None
    assert r.get_for("pair", "comp", "Tennis") is None


# ── reg.2: 挂 sport + comp,comp 内取 comp 策略,comp 外取 sport 策略 ──
def test_sport_and_competition_mounted_comp_wins_in_scope():
    r = StrategyRegistry()
    sport_s = _stub_strategy("sport:Soccer")
    comp_s = _stub_strategy("comp:EPL")
    r.register_sport("Soccer", sport_s)
    r.register_competition("EPL", comp_s)
    # EPL 内任意 pair → comp 策略
    assert r.get_for("epl_pair_1", "EPL", "Soccer") is comp_s
    # 同 sport 但不同 comp → sport 策略
    assert r.get_for("laliga_pair", "LaLiga", "Soccer") is sport_s


# ── reg.3: 三层都挂,挂载存在锁定不降级(Q21-a 核心)─────────────
def test_pair_mount_locks_scope_even_if_no_hit_downstream():
    """关键不变量:具体比赛挂了 → 返该 pair 策略,**即使** comp/sport 也挂了。
    "挂了就锁定本 scope" — 不降级测试见 evaluator slice(查 registry 返同一个就够)。"""
    r = StrategyRegistry()
    r.register_sport("Soccer", _stub_strategy("sport:Soccer"))
    r.register_competition("EPL", _stub_strategy("comp:EPL"))
    pair_s = _stub_strategy("pair:match_X")
    r.register_pair("match_X", pair_s)
    # 优先级 pair > comp > sport
    assert r.get_for("match_X", "EPL", "Soccer") is pair_s
    # 同 EPL 不同 pair → comp 策略(pair 挂的不影响别的 pair)
    assert r.get_for("match_Y", "EPL", "Soccer").scope_key == "comp:EPL"


# ── reg.4: 都没挂 → None(evaluator 端 no-op)──────────────────
def test_nothing_mounted_returns_none():
    r = StrategyRegistry()
    assert r.get_for("any_pair", "any_comp", "any_sport") is None


# ── 边界:任意层 None 参数都安全 ─────────────────────────────────
def test_none_arguments_safe():
    r = StrategyRegistry()
    r.register_sport("Soccer", _stub_strategy("s"))
    # pair_id / competition 给 None 不查它们;sport 命中
    assert r.get_for(None, None, "Soccer").scope_key == "s"
    # 全 None → None
    assert r.get_for(None, None, None) is None


# ── unregister 清除该 scope 挂载 ──────────────────────────────────
def test_unregister_removes_mount():
    r = StrategyRegistry()
    sport_s = _stub_strategy("sport:Soccer")
    pair_s = _stub_strategy("pair:match_X")
    r.register_sport("Soccer", sport_s)
    r.register_pair("match_X", pair_s)
    r.unregister_pair("match_X")
    # pair 挂载没了 → 降到 sport
    assert r.get_for("match_X", "EPL", "Soccer") is sport_s
    r.unregister_sport("Soccer")
    assert r.get_for("match_X", "EPL", "Soccer") is None
