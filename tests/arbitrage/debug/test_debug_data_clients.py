"""Q11.A DebugDataClient mixin 行为(`_handle_data` 拦截 + `_maybe_substitute` 钩子)。

对应用例:debug-A.{1,3,4}(substitute hook seam)。

不构造真 NT DataClient(它需要 ClobClient/msgbus/cache/clock 等):
用一个 `_RecorderBase` 假基类替代,聚焦验证 mixin 注入的行为。
"""

from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.debug.config import MockCategory
from src.arbitrage.debug.config import MockDataItem
from src.arbitrage.debug.data_clients import _DebugDataClientMixin


class _RecorderBase:
    """假基类:记 `_handle_data` 收到啥(替代 NT DataClient 父类的 msgbus 路由)。"""

    def __init__(self):
        self.received = []

    def _handle_data(self, data) -> None:
        self.received.append(data)


class _DebugClient(_DebugDataClientMixin, _RecorderBase):
    def __init__(self, debug):
        super().__init__()
        self._debug = debug


# ── debug-A.1: 默认 passthrough(`_maybe_substitute` 返 None,原 data 透传)─
def test_default_hook_passes_through():
    client = _DebugClient(debug=DebugConfig(enabled=False))
    client._handle_data("raw_delta")
    assert client.received == ["raw_delta"]


# ── debug-A.2: 子类覆盖 hook → 替换值进 super()._handle_data ──────
def test_overridden_hook_substitutes():
    class _Sub(_DebugClient):
        def _maybe_substitute(self, data):
            return f"mocked:{data}"

    client = _Sub(debug=DebugConfig(enabled=True))
    client._handle_data("raw_delta")
    assert client.received == ["mocked:raw_delta"]


# ── debug-A.3: 子类 hook 返 None → 退化到 passthrough(不会传 None 给 super)──
def test_hook_returning_none_falls_back_to_passthrough():
    class _Sub(_DebugClient):
        def _maybe_substitute(self, data):
            return None  # 显式 None

    client = _Sub(debug=DebugConfig(enabled=True))
    client._handle_data(42)
    assert client.received == [42]


# ── debug-A.4: 子类可经 self._debug 读 mock_data 决定替换 ────────
def test_hook_reads_debug_config_mock_data():
    cfg = DebugConfig(enabled=True)
    cfg.mock_data["odds_force"] = MockDataItem(
        id="odds_force",
        category=MockCategory.ODDS,
        enabled=True,
        data={"price": 0.5},
    )

    class _Sub(_DebugClient):
        def _maybe_substitute(self, data):
            mock = self._debug.get_mock(MockCategory.ODDS, {})
            return f"price={mock.data['price']}" if mock else None

    client = _Sub(debug=cfg)
    client._handle_data("ignored")
    assert client.received == ["price=0.5"]


# ── debug_config 访问器 ──────────────────────────────────────
def test_debug_config_accessor():
    cfg = DebugConfig(enabled=True)
    client = _DebugClient(debug=cfg)
    assert client.debug_config is cfg
