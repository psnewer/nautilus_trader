"""
DebugArbitrageLiveRiskEngine —— Q11.2:`skip_check_size` 覆盖。

启用 `skip_check_size` 时,**跳过 NT 父类 `_check_order`**(NT min_quantity / 价格 / GTD 校验),
**只跑应用层** `_check_balance` + `_check_rebate_gates`。用途:测试链路时让小于
`instrument.min_quantity` 的小单也过本地 RiskEngine(注意:venue 仍可能拒,看真实需求)。

P10 原则:**生产 `ArbitrageLiveRiskEngine` 零 `if debug`**;debug 行为通过本子类 + factory 选择实现。
"""

from __future__ import annotations

from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.risk.engine import ArbitrageLiveRiskEngine


class DebugArbitrageLiveRiskEngine(ArbitrageLiveRiskEngine):
    """`skip_check_size` 激活时跳过 NT 父类 `_check_order`,直跑应用层。"""

    def __init__(self, *args, debug: DebugConfig, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._debug = debug

    def _check_order(self, instrument, order) -> bool:
        if self._debug.is_override_active("skip_check_size"):
            # 跳过 NT 父类(price / quantity / GTD);只跑应用层
            if not self._check_balance(instrument, order):
                return False
            if not self._check_rebate_gates(order):
                return False
            return True
        return super()._check_order(instrument, order)
