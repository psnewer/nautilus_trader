"""SharpExch Playwright browser manager.

第一阶段复用 OE BrowserManager 实现,但在 SE adapter 子树提供独立导入入口,
避免后续 SE 专属行为散落到 OE 模块。
"""

from nautilus_trader.adapters.orbitexch.browser_manager import PlaywrightBrowserManager


__all__ = ["PlaywrightBrowserManager"]
