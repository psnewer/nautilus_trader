"""
默认策略

使用基类的默认方向选择逻辑。
"""

from .base import Strategy


class DefaultStrategy(Strategy):
    """
    默认策略

    使用基类的默认方向选择逻辑：
    1. 当前持仓返水率为负的一方（需要平衡持仓）
    2. 当前持仓返水率最小的一方
    3. rebate 信号计算出返水率最大的一方

    信号列表通过配置文件指定。
    """

    def __init__(self, name: str = "default", signals: list[str] | None = None):
        """
        初始化策略

        Args:
            name: 策略名称
            signals: 信号列表
        """
        self._name = name
        self._signals = signals or []

    @property
    def name(self) -> str:
        return self._name

    @property
    def signals(self) -> list[str]:
        return self._signals
