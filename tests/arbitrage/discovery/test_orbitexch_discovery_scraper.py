import asyncio
from types import SimpleNamespace

from nautilus_trader.adapters.orbitexch.discovery_scraper import OrbitExchScraper


class _FakeContext:
    def __init__(self):
        self.scripts = []

    async def add_init_script(self, script: str) -> None:
        self.scripts.append(script)


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_setup_stealth_installs_visibility_spoof_for_lazy_loaded_rows():
    """discovery-1.4.i:OE discovery 独立浏览器也要禁用 competition 页懒加载。"""
    scraper = OrbitExchScraper(SimpleNamespace(browser=SimpleNamespace(timeout_ms=1000)))
    context = _FakeContext()
    scraper._context = context

    _run(scraper._setup_stealth())

    assert len(context.scripts) == 1
    script = context.scripts[0]
    assert "navigator, 'webdriver'" in script
    assert "document, 'hidden'" in script
    assert "document, 'visibilityState'" in script
    assert "document.hasFocus" in script
    assert "IntersectionObserver" in script
    assert "intersectionRatio: 1.0" in script
    assert "isIntersecting: true" in script
