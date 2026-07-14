"""SE adapter 测试公共 fixture。"""

import pytest

import nautilus_trader.adapters.sharpexch.web as se_web


@pytest.fixture(autouse=True)
def _stub_post_login_popup_dismiss(monkeypatch):
    """打桩 `se_login` 末尾的弹窗关闭。

    真实实现按 deadline 分片轮询(默认 120s),fake page 缺 `locator`/弹窗永不出现时
    会把离线测试拖满整个预算。直测弹窗逻辑的用例持有原函数引用,不受影响。
    """

    async def _noop(page, *, timeout_ms: int = 0) -> bool:
        return False

    monkeypatch.setattr(se_web, "se_dismiss_post_login_popup", _noop)
