"""
Check / Action 类型 registry(slice 5,Q25)。

设计见 `docs/arbitrage/architectures/_cross-cutting/configuration.md §7.1`。

**框架层只提供 registry + builder,不预注册任何具体 Check/Action**;用户在 launcher main
顶部 `register_check("rebate", RebateCheck)` / `register_action("place_bet", PlaceBetAction)` 后,
JSON loader 才能按 `{"type": "rebate", "params": {...}}` 构造实例。

不预注册的理由:具体 Check/Action 是用户域(refactor.md #41 撤回旧策略平移);框架不绑死实现。
"""

from __future__ import annotations

from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import Check


class StrategyConfigError(Exception):
    """Strategy JSON 配置 / 解析失败(类型未注册 / 参数错 / binding 引用未知 id 等)。"""


_CHECK_REGISTRY: dict[str, type[Check]] = {}
_ACTION_REGISTRY: dict[str, type[Action]] = {}


def register_check(name: str, cls: type[Check]) -> None:
    """注册 Check 类型。重复注册同名同类幂等;同名异类 raise。"""
    existing = _CHECK_REGISTRY.get(name)
    if existing is not None and existing is not cls:
        raise StrategyConfigError(
            f"check type '{name}' already registered to {existing.__name__}, "
            f"cannot rebind to {cls.__name__}",
        )
    _CHECK_REGISTRY[name] = cls


def register_action(name: str, cls: type[Action]) -> None:
    """注册 Action 类型。同名异类 raise。"""
    existing = _ACTION_REGISTRY.get(name)
    if existing is not None and existing is not cls:
        raise StrategyConfigError(
            f"action type '{name}' already registered to {existing.__name__}, "
            f"cannot rebind to {cls.__name__}",
        )
    _ACTION_REGISTRY[name] = cls


def build_check(spec: dict) -> Check:
    """`{"type": "<name>", "params": {...}} → cls(**params)`;params 缺时默认 `{}`。"""
    if not isinstance(spec, dict) or "type" not in spec:
        raise StrategyConfigError(f"check spec must have 'type' key: {spec!r}")
    name = spec["type"]
    cls = _CHECK_REGISTRY.get(name)
    if cls is None:
        raise StrategyConfigError(
            f"unknown check type: '{name}' (registered: {sorted(_CHECK_REGISTRY.keys())})",
        )
    params = spec.get("params") or {}
    try:
        return cls(**params)
    except TypeError as exc:
        raise StrategyConfigError(f"invalid params for check type '{name}': {exc}") from exc


def build_action(spec: dict) -> Action:
    if not isinstance(spec, dict) or "type" not in spec:
        raise StrategyConfigError(f"action spec must have 'type' key: {spec!r}")
    name = spec["type"]
    cls = _ACTION_REGISTRY.get(name)
    if cls is None:
        raise StrategyConfigError(
            f"unknown action type: '{name}' (registered: {sorted(_ACTION_REGISTRY.keys())})",
        )
    params = spec.get("params") or {}
    try:
        return cls(**params)
    except TypeError as exc:
        raise StrategyConfigError(f"invalid params for action type '{name}': {exc}") from exc


def _reset_for_tests() -> None:
    """测试隔离用:清空两个 registry。生产路径不调。"""
    _CHECK_REGISTRY.clear()
    _ACTION_REGISTRY.clear()
