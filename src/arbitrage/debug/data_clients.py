"""
Debug DataClient 子类 —— Q11.A 行情数据掉包(子类化 + 工厂选择,P10)。

设计见 `architectures/_cross-cutting/debug-injection.md`。

行为:覆盖 `_handle_data(data)` —— 在数据进 NT DataEngine 之前看一眼,
根据 `DebugConfig.get_mock(MockCategory.ODDS, context)` 决定是否替换。

**框架职责:拦截 + seam**;**具体替换算法用户子类化覆盖** `_maybe_substitute(data)`
(默认 passthrough,返 None)。原因:mock_data 的 `data` 字段形态是用户场景特定的,
框架无法预设格式。用户在自己的 Debug 子类里实现"匹配 + 构造替换 OrderBookDeltas/Delta"。

PM / OE 两个子类除继承基类不同,机制完全对称;共用 `_maybe_substitute` 钩子。
"""

from __future__ import annotations

from nautilus_trader.adapters.polymarket.data import PolymarketDataClient

from nautilus_trader.adapters.orbitexch.data import OrbitExchDataClient

from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.debug.config import MockCategory


class _DebugDataClientMixin:
    """共享行为:拦 `_handle_data` + `_maybe_substitute` 钩子。

    子类必须把 `_debug: DebugConfig` 存到 `self._debug`(由具体 PM/OE 子类 __init__ 注入)。
    """

    def _handle_data(self, data) -> None:
        substituted = self._maybe_substitute(data)
        # noinspection PyUnresolvedReferences
        super()._handle_data(substituted if substituted is not None else data)

    def _maybe_substitute(self, data):
        """Hook:返回替换后的 data;返 None 走 passthrough。

        **默认 passthrough**(框架占位);**用户子类化覆盖**按具体 mock_data schema 实现:
        ```python
        class MyDebugPMDataClient(DebugPolymarketDataClient):
            def _maybe_substitute(self, data):
                if not isinstance(data, OrderBookDeltas):
                    return None
                mock = self._debug.get_mock(MockCategory.ODDS,
                                            context={"instrument_id": str(data.instrument_id)})
                if mock is None:
                    return None
                # 按 mock.data 字段构造替换的 OrderBookDeltas...
                return _build_mock_deltas(data.instrument_id, mock.data, ts=...)
        ```
        """
        return None  # 默认 passthrough(子类未覆盖时,DebugXxxDataClient 等同生产)

    @property
    def debug_config(self) -> DebugConfig:
        # noinspection PyUnresolvedReferences
        return self._debug


class DebugPolymarketDataClient(_DebugDataClientMixin, PolymarketDataClient):
    """PM 数据客户端 Debug 子类。覆盖 `_handle_data` 拦数据;`_maybe_substitute` 默认 passthrough。"""

    def __init__(self, *args, debug: DebugConfig, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._debug = debug


class DebugOrbitExchDataClient(_DebugDataClientMixin, OrbitExchDataClient):
    """OE 数据客户端 Debug 子类。机制同 PM(共享 mixin)。"""

    def __init__(self, *args, debug: DebugConfig, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._debug = debug
