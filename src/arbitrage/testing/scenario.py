"""
TestScenario 基类

声明式描述一次测试：
- name / description: 标识与说明
- debug_overrides: 部分覆盖 debug_config 的默认值（dict[str, tuple[bool, Any]]）
- strategy: 策略名（也走 debug 覆盖机制，等价于 debug_overrides["strategy_name"]）
- success: 成功条件（Condition）
- failure: 失败条件（Condition；可选）
- timeout_sec: 超时（秒）

子类只需以类属性覆盖即可，不需要 __init__：

    class PlaceAndCancelScenario(TestScenario):
        name = "place_and_cancel"
        debug_overrides = {
            "polymarket_price": (True, 0.01),
            ...
        }
        strategy = "max-rebate"
        success = Sequence(...)
        failure = AnyOf(...)
        timeout_sec = 300
"""

from __future__ import annotations

from typing import Any, ClassVar

from .conditions import Condition


class TestScenario:
    """测试场景基类"""

    name: ClassVar[str] = "unnamed"
    description: ClassVar[str] = ""

    # 部分覆盖: name -> (enabled, value)。enabled=None 时只改值不改 enabled。
    debug_overrides: ClassVar[dict[str, tuple[bool | None, Any]]] = {}

    # 策略名（语法糖；等价于 debug_overrides["strategy_name"] = (True, strategy)）
    strategy: ClassVar[str | None] = None

    # 成功 / 失败条件
    success: ClassVar[Condition | None] = None
    failure: ClassVar[Condition | None] = None

    # 超时（秒）。0 / None 表示无超时（不建议）
    timeout_sec: ClassVar[float] = 300.0

    # 是否在退出时打印完整捕获日志（默认只打印关键事件）
    verbose_report: ClassVar[bool] = False

    # 启动 web_gateway 后是否自动调 POST /api/pipeline/start 触发全链路
    # 子类如需自定义启动序列，覆盖 async def setup(self, runner) 即可
    auto_start_pipeline: ClassVar[bool] = True

    def __init__(self) -> None:
        # 实例化时复制类属性供 runner 改动而不污染类
        self._overrides: dict[str, tuple[bool | None, Any]] = dict(self.debug_overrides)
        if self.strategy:
            # strategy 翻译成 strategy_name 覆盖
            self._overrides["strategy_name"] = (True, self.strategy)

    @property
    def effective_overrides(self) -> dict[str, tuple[bool | None, Any]]:
        return self._overrides

    def get_success(self) -> Condition | None:
        """子类可覆盖以动态生成 condition"""
        return self.__class__.success

    def get_failure(self) -> Condition | None:
        return self.__class__.failure
