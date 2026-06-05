"""Debug 注入框架(Q11 / §6.6 / P10)。

**P10 原则**:所有 debug 行为变化通过**子类化 + 工厂层选择**实现,生产代码零 `if self._debug`。
配置经 `DebugConfig`(普通对象)+ `ArbContext.debug_config`(DI)传递,**不进 NT Cache**(Q11.5)。
"""

from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.debug.config import DebugOverride
from src.arbitrage.debug.config import MockCategory
from src.arbitrage.debug.config import MockDataItem
from src.arbitrage.debug.risk import DebugArbitrageLiveRiskEngine

__all__ = [
    "DebugArbitrageLiveRiskEngine",
    "DebugConfig",
    "DebugOverride",
    "MockCategory",
    "MockDataItem",
]
