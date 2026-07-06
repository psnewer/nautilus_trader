"""
DebugConfig —— Q11 Debug 注入框架的配置层(普通对象,DI 注入,**不是单例**)。

设计原则(refactor.md §6.6 + Q11.5):
- **不进 NT Cache**:`DebugConfig` 是普通 Python 对象,通过 `ArbContext.debug_config` DI 到
  各 factory,**不**经 `Cache.add(key, bytes)` / msgbus 走 NT 序列化通道。
- **DI 替代单例**(撤回旧 `DebugManager` 单例):每个进程一个 `DebugConfig` 实例,
  各 factory / Debug 子类构造时显式接收;无全局 `debug_manager` 模块级单例。
- 配置文件形态保持兼容(`enabled` + `overrides` + `mock_data`),用户的 `debug_config.json` 不动。

字段 schema:
```json
{
  "enabled": true,
  "overrides": { "<name>": { "enabled": bool, "value": Any }, ... },
  "mock_data": { "<id>": { "category": str, "name": str, "enabled": bool,
                            "data": Any, "conditions": {...}, "priority": int }, ... }
}
```

消费方:Debug 子类(如 `DebugArbitrageLiveRiskEngine`)接 `debug: DebugConfig`,内部用
`debug.is_override_active(name)` / `debug.get_override_value(name)` / `debug.get_mock(...)`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any


class MockCategory(Enum):
    """模拟数据分类(消费方按 category 取)。"""

    ODDS = "odds"
    ORDERS = "orders"
    POSITIONS = "positions"
    OPPORTUNITIES = "opportunities"
    EXECUTION = "execution"
    MARKET = "market"
    ACCOUNT = "account"
    TIMELINE = "timeline"  # 订单状态时序模拟(部分填/拒单/撤单)


@dataclass
class DebugOverride:
    """单条覆盖项:name + (enabled + value)。"""

    name: str
    enabled: bool = False
    value: Any = None
    description: str = ""


@dataclass
class MockDataItem:
    """单条模拟数据:category + data + conditions(匹配上下文)+ priority。"""

    id: str
    category: MockCategory
    name: str = ""
    enabled: bool = False
    data: Any = None
    conditions: dict = field(default_factory=dict)
    priority: int = 0

    def matches(self, context: dict) -> bool:
        if not self.enabled:
            return False
        if not self.conditions:
            return True
        return all(context.get(k) == v for k, v in self.conditions.items())


@dataclass
class DebugConfig:
    """全栈 debug 配置。`enabled=False` 时所有 override / mock 都视为未激活。"""

    enabled: bool = False
    overrides: dict[str, DebugOverride] = field(default_factory=dict)
    mock_data: dict[str, MockDataItem] = field(default_factory=dict)

    # ── override 读 ─────────────────────────────────────────────────
    def is_override_active(self, name: str) -> bool:
        """总开关 + 该项 enabled 才算激活。"""
        if not self.enabled:
            return False
        ovr = self.overrides.get(name)
        return ovr is not None and ovr.enabled

    def get_override_value(self, name: str, default: Any = None) -> Any:
        """未激活返 default(消费方按 `is_override_active` 守门更明确)。"""
        if not self.is_override_active(name):
            return default
        return self.overrides[name].value

    # ── mock 数据读(按 category + conditions 匹配,priority 高的优先)──
    def get_mock(self, category: MockCategory, context: dict | None = None) -> MockDataItem | None:
        if not self.enabled:
            return None
        ctx = context or {}
        matches = [m for m in self.mock_data.values() if m.category == category and m.matches(ctx)]
        if not matches:
            return None
        matches.sort(key=lambda m: m.priority, reverse=True)
        return matches[0]

    def get_mock_data(self, category: MockCategory, context: dict | None = None) -> Any:
        item = self.get_mock(category, context)
        return item.data if item else None

    # ── 序列化 / 文件 ───────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "overrides": {
                k: {"enabled": v.enabled, "value": v.value, "description": v.description}
                for k, v in self.overrides.items()
            },
            "mock_data": {
                k: {
                    "category": v.category.value, "name": v.name, "enabled": v.enabled,
                    "data": v.data, "conditions": v.conditions, "priority": v.priority,
                }
                for k, v in self.mock_data.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DebugConfig":
        cfg = cls(enabled=bool(data.get("enabled", False)))
        for name, raw in (data.get("overrides") or {}).items():
            cfg.overrides[name] = DebugOverride(
                name=name,
                enabled=bool(raw.get("enabled", False)),
                value=raw.get("value"),
                description=raw.get("description", ""),
            )
        for mid, raw in (data.get("mock_data") or {}).items():
            cfg.mock_data[mid] = MockDataItem(
                id=mid,
                category=MockCategory(raw["category"]),
                name=raw.get("name", ""),
                enabled=bool(raw.get("enabled", False)),
                data=raw.get("data"),
                conditions=raw.get("conditions") or {},
                priority=int(raw.get("priority", 0)),
            )
        return cfg

    @classmethod
    def load(cls, path: str) -> "DebugConfig":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
