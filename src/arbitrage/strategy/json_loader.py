"""
Strategy JSON loader(slice 5,Q25)—— JSON 描述 → Q21 in-memory 对象。

设计见 `docs/arbitrage/architectures/_cross-cutting/configuration.md §7`。

3 个递归解析函数 + 1 个装配函数 + 1 个 registry builder:

  bool_expr_from_json(spec)    → BoolExpr             递归 {AND/OR/NOT/signal}
  condition_from_json(spec)    → Condition            递归 sub_conditions
  strategy_from_json(...)      → Strategy             arbitrage_tree + compensation_tree
  build_strategy_registry(cfg) → StrategyRegistry     消费 ArbConfig.strategy.{strategies, bindings}

关键缺值处理(Q21 必填字段):
- `self_hits` 缺 / None → `AndExpr()`(空 AND = 真值兜底,让 sub_conditions/checktion 决定 hit)
- `compensation_tree` 缺 / None → 永 False no-op Condition(`OrExpr()` 空 OR = False)
"""

from __future__ import annotations

from src.arbitrage.strategy.bool_expr import AndExpr
from src.arbitrage.strategy.bool_expr import BoolExpr
from src.arbitrage.strategy.bool_expr import NotExpr
from src.arbitrage.strategy.bool_expr import OrExpr
from src.arbitrage.strategy.bool_expr import SignalRef
from src.arbitrage.strategy.check_action_registry import StrategyConfigError
from src.arbitrage.strategy.check_action_registry import build_action
from src.arbitrage.strategy.check_action_registry import build_check
from src.arbitrage.strategy.condition import Condition
from src.arbitrage.strategy.registry import Strategy
from src.arbitrage.strategy.registry import StrategyRegistry

# 默认值兜底(模块级常量,避免每次 new)
_DEFAULT_SELF_HITS = AndExpr()      # 空 AND → True(self_hits 缺时让下游决定)
_DEFAULT_NEVER_HIT = OrExpr()       # 空 OR → False(compensation 缺时永不 fire)
_NOOP_COMPENSATION_TREE = Condition(self_hits=_DEFAULT_NEVER_HIT)


def bool_expr_from_json(spec) -> BoolExpr:
    """{"signal": "name"} / {"AND": [...]} / {"OR": [...]} / {"NOT": <sub>} 递归。

    None / 空 dict / 空字符串 → `AndExpr()`(真值兜底)。
    """
    if spec is None or spec == {} or spec == "":
        return _DEFAULT_SELF_HITS

    if not isinstance(spec, dict):
        raise StrategyConfigError(f"bool_expr spec must be dict, got {type(spec).__name__}: {spec!r}")

    # 互斥:一条 spec 只能含一个 key
    keys = list(spec.keys())
    if len(keys) != 1:
        raise StrategyConfigError(
            f"bool_expr spec must have exactly one of AND/OR/NOT/signal, got {keys}",
        )
    key = keys[0]
    val = spec[key]

    if key == "signal":
        if not isinstance(val, str):
            raise StrategyConfigError(f"signal name must be str, got {val!r}")
        return SignalRef(val)
    if key == "AND":
        if not isinstance(val, list):
            raise StrategyConfigError(f"AND value must be list, got {val!r}")
        return AndExpr(*[bool_expr_from_json(s) for s in val])
    if key == "OR":
        if not isinstance(val, list):
            raise StrategyConfigError(f"OR value must be list, got {val!r}")
        return OrExpr(*[bool_expr_from_json(s) for s in val])
    if key == "NOT":
        return NotExpr(bool_expr_from_json(val))

    raise StrategyConfigError(
        f"unknown bool_expr key '{key}' (expected AND/OR/NOT/signal)",
    )


def condition_from_json(spec) -> Condition:
    """递归解 Condition 树。

    spec 结构:
      {
        "self_hits":      <bool_expr_spec> | None,
        "sub_conditions": [<condition_spec>, ...],
        "checktion":      [{"type": ..., "params": {...}}, ...],
        "actions":        [{"type": ..., "params": {...}}, ...],  # 依次执行
        "action":         {"type": ..., "params": {...}} | None,  # 兼容旧格式(转 actions=[action])
      }
    """
    if not isinstance(spec, dict):
        raise StrategyConfigError(f"condition spec must be dict, got {type(spec).__name__}: {spec!r}")

    self_hits = bool_expr_from_json(spec.get("self_hits"))

    sub_specs = spec.get("sub_conditions") or []
    if not isinstance(sub_specs, list):
        raise StrategyConfigError(f"sub_conditions must be list, got {sub_specs!r}")
    sub_conditions = [condition_from_json(s) for s in sub_specs]

    check_specs = spec.get("checktion") or []
    if not isinstance(check_specs, list):
        raise StrategyConfigError(f"checktion must be list, got {check_specs!r}")
    checktion = [build_check(c) for c in check_specs]

    # 兼容两种格式: "actions": [...] (新) 或 "action": {...} (旧)
    actions_spec = spec.get("actions") or []
    if not actions_spec and spec.get("action"):  # 兼容旧格式
        actions_spec = [spec.get("action")]
    if not isinstance(actions_spec, list):
        raise StrategyConfigError(f"actions must be list, got {actions_spec!r}")
    actions = [build_action(a) for a in actions_spec]

    return Condition(
        self_hits=self_hits,
        sub_conditions=sub_conditions,
        checktion=checktion,
        actions=actions,
    )


def strategy_from_json(strategy_id: str, spec, scope_key: str) -> Strategy:
    """装配 Strategy(scope_key + arbitrage_tree + compensation_tree)。

    `compensation_tree` 缺 → 永 False no-op Condition(不 fire 任何补救 Action)。
    `spec` 接受 dict 或 `StrategyJsonConfig`(msgspec Struct;取 attr)。
    """
    if hasattr(spec, "arbitrage_tree"):
        # msgspec StrategyJsonConfig
        arb_spec = spec.arbitrage_tree
        comp_spec = spec.compensation_tree
        description = getattr(spec, "description", "")
    elif isinstance(spec, dict):
        arb_spec = spec.get("arbitrage_tree")
        comp_spec = spec.get("compensation_tree")
        description = spec.get("description", "")
    else:
        raise StrategyConfigError(f"strategy '{strategy_id}' spec invalid: {spec!r}")

    if arb_spec is None:
        raise StrategyConfigError(
            f"strategy '{strategy_id}' missing arbitrage_tree (required)",
        )

    arbitrage_tree = condition_from_json(arb_spec)
    compensation_tree = (
        condition_from_json(comp_spec) if comp_spec is not None else _NOOP_COMPENSATION_TREE
    )

    metadata = {"id": strategy_id}
    if description:
        metadata["description"] = description

    return Strategy(
        scope_key=scope_key,
        arbitrage_tree=arbitrage_tree,
        compensation_tree=compensation_tree,
        metadata=metadata,
    )


def build_strategy_registry(strategy_section) -> StrategyRegistry:
    """消费 ArbConfig.strategy(`StrategySectionConfig`):构造所有 Strategy 实例,按 bindings 装载。

    scope 字符串格式:`pair:<id>` / `competition:<name>` / `sport:<name>`(支持 `pair_id:` 别名)。
    """
    registry = StrategyRegistry()

    # 1. JSON spec → Strategy 实例(metadata 含 id;scope 在 binding 时确定)
    strategies = strategy_section.strategies if hasattr(strategy_section, "strategies") else {}
    bindings = strategy_section.bindings if hasattr(strategy_section, "bindings") else []

    for binding in bindings:
        scope = binding.scope if hasattr(binding, "scope") else binding["scope"]
        sid = binding.strategy_id if hasattr(binding, "strategy_id") else binding["strategy_id"]

        if sid not in strategies:
            raise StrategyConfigError(
                f"binding references unknown strategy_id: '{sid}' "
                f"(available: {sorted(strategies.keys())})",
            )

        strategy = strategy_from_json(sid, strategies[sid], scope_key=scope)
        kind, _, name = scope.partition(":")
        if not name:
            raise StrategyConfigError(f"binding scope must be '<kind>:<name>', got '{scope}'")

        if kind in ("pair", "pair_id"):
            registry.register_pair(name, strategy)
        elif kind == "competition":
            registry.register_competition(name, strategy)
        elif kind == "sport":
            registry.register_sport(name, strategy)
        else:
            raise StrategyConfigError(
                f"binding scope kind must be pair/competition/sport, got '{kind}'",
            )

    return registry
