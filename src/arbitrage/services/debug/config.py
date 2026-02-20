"""
Debug 配置

单一配置文件，统一管理：
- 变量覆盖 (overrides): 修改运行时变量
- 测试数据 (mock_data): 注入虚假数据

所有配置项通过 enabled 字段控制是否生效。
"""

from dataclasses import dataclass, field
from typing import Any
from enum import Enum
import json
import logging
from pathlib import Path


class OverrideTarget(Enum):
    """覆盖目标分类"""
    STRATEGY = "strategy"      # 策略相关
    EXECUTION = "execution"    # 执行相关
    ODDS = "odds"              # 赔率相关


class MockCategory(Enum):
    """模拟数据分类"""
    ODDS = "odds"                    # 赔率数据
    ORDERS = "orders"                # 订单数据
    POSITIONS = "positions"          # 持仓数据
    OPPORTUNITIES = "opportunities"  # 套利机会
    EXECUTION = "execution"          # 执行结果
    MARKET = "market"                # 市场数据
    ACCOUNT = "account"              # 账户数据


@dataclass
class DebugOverride:
    """变量覆盖配置"""
    name: str
    description: str
    target: OverrideTarget
    enabled: bool = False
    value: Any = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "target": self.target.value,
            "enabled": self.enabled,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DebugOverride":
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            target=OverrideTarget(data.get("target", "strategy")),
            enabled=data.get("enabled", False),
            value=data.get("value"),
        )


@dataclass
class MockDataItem:
    """模拟数据项"""
    id: str
    category: MockCategory
    name: str
    enabled: bool = False
    data: Any = None
    conditions: dict = field(default_factory=dict)
    priority: int = 0

    def matches(self, context: dict) -> bool:
        """检查是否匹配给定上下文"""
        if not self.enabled:
            return False
        if not self.conditions:
            return True
        for key, value in self.conditions.items():
            if context.get(key) != value:
                return False
        return True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category.value,
            "name": self.name,
            "enabled": self.enabled,
            "data": self.data,
            "conditions": self.conditions,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MockDataItem":
        return cls(
            id=data["id"],
            category=MockCategory(data["category"]),
            name=data["name"],
            enabled=data.get("enabled", False),
            data=data.get("data"),
            conditions=data.get("conditions", {}),
            priority=data.get("priority", 0),
        )


@dataclass
class DebugConfig:
    """
    Debug 统一配置

    示例配置文件:
    ```json
    {
        "enabled": true,
        "overrides": {
            "min_rebate_rate": {"enabled": true, "value": -10.0},
            "polymarket_price": {"enabled": true, "value": 0.01}
        },
        "mock_data": {
            "test_position_1": {
                "category": "positions",
                "name": "测试持仓",
                "enabled": true,
                "data": {...}
            }
        }
    }
    ```
    """
    # 总开关
    enabled: bool = False

    # 变量覆盖
    overrides: dict[str, DebugOverride] = field(default_factory=dict)

    # 模拟数据
    mock_data: dict[str, MockDataItem] = field(default_factory=dict)

    # 配置文件路径
    config_file: str = ""

    def __post_init__(self):
        if not self.overrides:
            self.overrides = self._get_default_overrides()

    @staticmethod
    def _get_default_overrides() -> dict[str, DebugOverride]:
        """默认覆盖配置"""
        return {
            # 策略相关
            "min_rebate_rate": DebugOverride(
                name="min_rebate_rate",
                description="最小返水率 (设为负数强制触发)",
                target=OverrideTarget.STRATEGY,
                value=-10.0,
            ),
            "force_opportunity": DebugOverride(
                name="force_opportunity",
                description="强制生成套利机会",
                target=OverrideTarget.STRATEGY,
                value=True,
            ),
            # 执行相关
            "polymarket_price": DebugOverride(
                name="polymarket_price",
                description="Polymarket 订单价格 (0.01=1%概率，不会成交)",
                target=OverrideTarget.EXECUTION,
                value=0.01,
            ),
            "orbitexch_price": DebugOverride(
                name="orbitexch_price",
                description="OrbitExch 订单价格 (1000=0.1%概率，不会成交)",
                target=OverrideTarget.EXECUTION,
                value=1000.0,
            ),
            "order_size": DebugOverride(
                name="order_size",
                description="订单金额覆盖（通用）",
                target=OverrideTarget.EXECUTION,
                value=1.0,
            ),
            "polymarket_size": DebugOverride(
                name="polymarket_size",
                description="Polymarket 订单金额覆盖",
                target=OverrideTarget.EXECUTION,
                value=0.01,
            ),
            "orbitexch_size": DebugOverride(
                name="orbitexch_size",
                description="OrbitExch 订单金额覆盖",
                target=OverrideTarget.EXECUTION,
                value=10.0,
            ),
            "skip_execution": DebugOverride(
                name="skip_execution",
                description="跳过实际执行 (仅记录)",
                target=OverrideTarget.EXECUTION,
                value=True,
            ),
            "use_mock_exchange": DebugOverride(
                name="use_mock_exchange",
                description="使用模拟交易所（仅在 Debug 模式下生效）",
                target=OverrideTarget.EXECUTION,
                value=False,
            ),
        }

    # =========================================================================
    # 覆盖操作
    # =========================================================================

    def is_override_active(self, name: str) -> bool:
        """检查覆盖是否激活"""
        if not self.enabled:
            return False
        override = self.overrides.get(name)
        return override is not None and override.enabled

    def get_override_value(self, name: str, default: Any = None) -> Any:
        """获取覆盖值"""
        if not self.is_override_active(name):
            return default
        return self.overrides[name].value

    def set_override(self, name: str, enabled: bool = None, value: Any = None) -> bool:
        """设置覆盖"""
        override = self.overrides.get(name)
        if not override:
            return False
        if enabled is not None:
            override.enabled = enabled
        if value is not None:
            override.value = value
        return True

    # =========================================================================
    # Mock 数据操作
    # =========================================================================

    def add_mock(self, item: MockDataItem) -> None:
        """添加模拟数据"""
        self.mock_data[item.id] = item

    def get_mock(self, category: MockCategory, context: dict = None) -> MockDataItem | None:
        """获取匹配的模拟数据"""
        if not self.enabled:
            return None
        context = context or {}
        matches = [
            item for item in self.mock_data.values()
            if item.category == category and item.matches(context)
        ]
        if not matches:
            return None
        matches.sort(key=lambda x: x.priority, reverse=True)
        return matches[0]

    def get_mock_data(self, category: MockCategory, context: dict = None) -> Any:
        """获取模拟数据内容"""
        item = self.get_mock(category, context)
        return item.data if item else None

    def get_or_default(self, category: MockCategory, default: Any, context: dict = None) -> Any:
        """获取模拟数据或返回默认值"""
        data = self.get_mock_data(category, context)
        return data if data is not None else default

    # =========================================================================
    # 序列化
    # =========================================================================

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "overrides": {
                k: {"enabled": v.enabled, "value": v.value}
                for k, v in self.overrides.items()
            },
            "mock_data": {k: v.to_dict() for k, v in self.mock_data.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DebugConfig":
        config = cls(enabled=data.get("enabled", False))

        # 加载覆盖
        for name, override_data in data.get("overrides", {}).items():
            if name in config.overrides:
                config.overrides[name].enabled = override_data.get("enabled", False)
                if "value" in override_data:
                    config.overrides[name].value = override_data["value"]

        # 加载模拟数据
        for item_id, item_data in data.get("mock_data", {}).items():
            item_data["id"] = item_id
            config.mock_data[item_id] = MockDataItem.from_dict(item_data)

        return config

    def save(self, path: str = None) -> None:
        """保存到文件"""
        path = path or self.config_file
        if not path:
            raise ValueError("No config file path")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        self.config_file = path

    @classmethod
    def load(cls, path: str) -> "DebugConfig":
        """从文件加载"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        config = cls.from_dict(data)
        config.config_file = path
        return config


class DebugManager:
    """
    Debug 管理器（单例）

    使用示例:
        from src.arbitrage.services.debug import debug_manager

        # 加载配置
        debug_manager.load("debug_config.json")

        # 获取覆盖值
        price = debug_manager.get_override("polymarket_price", default=real_price)

        # 获取模拟数据
        positions = debug_manager.get_mock_data(MockCategory.POSITIONS)
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._config = DebugConfig()
        self._log = logging.getLogger("DebugManager")
        self._initialized = True

    @property
    def config(self) -> DebugConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def enable(self) -> None:
        """启用 debug 模式"""
        self._config.enabled = True
        self._log.warning("DEBUG MODE ENABLED")

    def disable(self) -> None:
        """禁用 debug 模式"""
        self._config.enabled = False
        self._log.info("Debug mode disabled")

    def load(self, path: str) -> bool:
        """加载配置文件"""
        try:
            self._config = DebugConfig.load(path)
            self._log.info(f"Loaded debug config from {path}")
            if self._config.enabled:
                self._log.warning("DEBUG MODE ENABLED from config")
            return True
        except Exception as e:
            self._log.error(f"Failed to load config: {e}")
            return False

    def save(self, path: str = None) -> bool:
        """保存配置文件"""
        try:
            self._config.save(path)
            return True
        except Exception as e:
            self._log.error(f"Failed to save config: {e}")
            return False

    # 覆盖操作
    def get_override(self, name: str, default: Any = None) -> Any:
        """获取覆盖值"""
        return self._config.get_override_value(name, default)

    def set_override(self, name: str, enabled: bool = None, value: Any = None) -> bool:
        """设置覆盖"""
        return self._config.set_override(name, enabled, value)

    def is_override_active(self, name: str) -> bool:
        """检查覆盖是否激活"""
        return self._config.is_override_active(name)

    # Mock 数据操作
    def get_mock(self, category: MockCategory, context: dict = None) -> MockDataItem | None:
        """获取模拟数据"""
        return self._config.get_mock(category, context)

    def get_mock_data(self, category: MockCategory, context: dict = None) -> Any:
        """获取模拟数据内容"""
        return self._config.get_mock_data(category, context)

    def get_or_default(self, category: MockCategory, default: Any, context: dict = None) -> Any:
        """获取模拟数据或默认值"""
        return self._config.get_or_default(category, default, context)

    def add_mock(
        self,
        item_id: str,
        category: MockCategory,
        name: str,
        data: Any,
        enabled: bool = True,
        conditions: dict = None,
    ) -> None:
        """添加模拟数据"""
        item = MockDataItem(
            id=item_id,
            category=category,
            name=name,
            enabled=enabled,
            data=data,
            conditions=conditions or {},
        )
        self._config.add_mock(item)

    def get_status(self) -> dict:
        """获取状态"""
        active_overrides = [
            name for name, o in self._config.overrides.items()
            if o.enabled
        ]
        enabled_mocks = [
            item_id for item_id, item in self._config.mock_data.items()
            if item.enabled
        ]
        return {
            "enabled": self._config.enabled,
            "config_file": self._config.config_file,
            "active_overrides": active_overrides,
            "enabled_mocks": enabled_mocks,
        }


# 全局单例
debug_manager = DebugManager()
