"""PlaywrightBrowserManager 并发启动安全。

OE data/exec 共享 manager 与 page 命名由 factory/runtime 测试覆盖;本文件只保留
2026-06-21 SIGABRT 修复对应的低层并发 `start()` 回归测试。
"""


import asyncio

from nautilus_trader.common.browser import browser_manager as _bm_mod
from nautilus_trader.common.browser import PlaywrightBrowserManager


def test_concurrent_start_launches_browser_once(monkeypatch):
    """oe-adapter-2.5: NT 同时连 OE Data+Exec → 共享 BrowserManager 的两个 start() 并发,
    必须只真正 launch 一次浏览器(否则并发双开 Chromium → macOS crashpad SIGABRT)。"""
    launch_count = {"n": 0}

    class _FakeContext:
        async def new_page(self):
            return object()

    class _FakeBrowser:
        async def new_context(self, **kw):
            return _FakeContext()

    class _FakeChromium:
        async def launch(self, **kw):
            launch_count["n"] += 1
            await asyncio.sleep(0.05)  # 放大竞态窗口:第一个 start 还没置 _context 时第二个就进来
            return _FakeBrowser()

    class _FakePlaywright:
        chromium = _FakeChromium()

        async def stop(self):
            pass

    class _FakePWManager:
        async def start(self):
            return _FakePlaywright()

    monkeypatch.setattr(_bm_mod, "async_playwright", lambda: _FakePWManager())

    bm = PlaywrightBrowserManager(headless=True)

    async def _no_stealth():
        return None

    monkeypatch.setattr(bm, "_setup_stealth", _no_stealth)

    async def run():
        await asyncio.gather(bm.start(), bm.start(), bm.start())

    asyncio.run(run())
    assert launch_count["n"] == 1
