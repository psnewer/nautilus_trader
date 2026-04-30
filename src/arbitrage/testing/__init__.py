"""
套利系统测试框架

实盘/模拟盘场景化测试框架，配合 debug_config.json 的变量掉包机制完成测试。

核心组件:
- TestScenario: 声明式描述一次测试（覆盖、策略、退出条件、超时）
- LogMonitor: 注册 logging.Handler，把 LogRecord 流式喂给条件引擎
- conditions: 退出条件原语（LogMatch/AllOf/AnyOf/Sequence/Negate/Timeout）
- ScenarioRunner: 启动 web_gateway, 挂监听, 评估, 退出, 出报告

使用示例:
    python -m src.arbitrage.testing --scenario place_and_cancel
"""

from .conditions import (
    AllOf,
    AnyOf,
    Condition,
    ConditionResult,
    LogMatch,
    Negate,
    Sequence,
)
from .monitor import LogEvent, LogMonitor
from .runner import ScenarioRunner
from .scenario import TestScenario

__all__ = [
    "AllOf",
    "AnyOf",
    "Condition",
    "ConditionResult",
    "LogEvent",
    "LogMatch",
    "LogMonitor",
    "Negate",
    "ScenarioRunner",
    "Sequence",
    "TestScenario",
]
