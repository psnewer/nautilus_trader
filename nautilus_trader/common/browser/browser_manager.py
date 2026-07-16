# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
# -------------------------------------------------------------------------------------------------

"""Playwright browser manager for browser-based venue adapters (OE/SE etc.)."""

import asyncio
import logging
from typing import Dict, Optional

try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
except ImportError:
    raise ImportError(
        'Playwright is required for browser-based adapters. '
        'Install it with: pip install playwright && playwright install chromium'
    )


class PlaywrightBrowserManager:
    """
    Manages Playwright browser instances for browser-based venue adapters.

    Features
    --------
    - Browser lifecycle management
    - Session persistence
    - Anti-detection measures
    - Page management
    
    Parameters
    ----------
    browser_type : str, default 'chromium'
        Browser type: 'chromium', 'firefox', or 'webkit'
    headless : bool, default True
        Run in headless mode
    user_data_dir : str, optional
        Directory for persistent browser data
    """
    
    def __init__(
        self,
        browser_type: str = 'chromium',
        headless: bool = True,
        user_data_dir: Optional[str] = None,
    ):
        self.browser_type = browser_type
        self.headless = headless
        self.user_data_dir = user_data_dir
        
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._pages: Dict[str, Page] = {}
        self._start_lock = asyncio.Lock()  # 防并发首次 start 双开浏览器(见 start 注释)

        self._log = logging.getLogger(self.__class__.__name__)

    async def start(self) -> None:
        """Start the browser. Idempotent + **并发安全** —— 已 start 时 no-op(slice 10c smoke 发现:
        OE Data/Exec 共享 BrowserManager,先连的会 start;后连的 start 应不重复)。

        ⚠️ 并发锁(2026-06-21):`_context` 只在 `await launch()` **完成后**才置位;NT 同时连
        OE Data + Exec → 两个 `start()` 协程都能越过 `if _context is not None` 守卫(此刻仍 None)
        → **并发双开 Chromium**,macOS 上 crashpad 竞争 → SIGABRT(`TargetClosedError`)。用 lock
        串行首次 start,第二个进锁后 double-check 见 `_context` 已置位即返回。"""
        if self._context is not None:
            return  # 已起(快路径,无需取锁)
        async with self._start_lock:
            if self._context is not None:
                return  # 进锁后复检:前一个 start 已完成
            self._log.info('🚀 Starting Playwright browser...')
            await self._do_start()

    async def _do_start(self) -> None:

        # Start Playwright
        self._playwright = await async_playwright().start()
        
        # Select browser
        if self.browser_type == 'chromium':
            launcher = self._playwright.chromium
        elif self.browser_type == 'firefox':
            launcher = self._playwright.firefox
        elif self.browser_type == 'webkit':
            launcher = self._playwright.webkit
        else:
            raise ValueError(f'Unknown browser type: {self.browser_type}')
        
        # Launch options
        launch_args = [
            '--disable-blink-features=AutomationControlled',
            '--disable-dev-shm-usage',
            '--no-sandbox',
        ]
        
        # Launch with persistent context if user_data_dir specified
        if self.user_data_dir:
            self._log.info(f'📁 Using persistent context: {self.user_data_dir}')
            
            self._context = await launcher.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=self.headless,
                args=launch_args,
                viewport={'width': 1920, 'height': 1080},
            )
            self._browser = self._context.browser
        else:
            self._browser = await launcher.launch(
                headless=self.headless,
                args=launch_args,
            )
            self._context = await self._browser.new_context(
                viewport={'width': 1920, 'height': 1080},
            )
        
        # Anti-detection
        await self._setup_stealth()

        # Close default blank page created by launch_persistent_context
        if self.user_data_dir and self._context.pages:
            default_page = self._context.pages[0]
            if default_page.url in ("about:blank", ""):
                await default_page.close()
                self._log.debug("Closed default blank page")

        self._log.info('✅ Browser started successfully')

    @property
    def context(self) -> Optional[BrowserContext]:
        """Return the active Playwright browser context, if started."""

        return self._context
    
    async def _setup_stealth(self) -> None:
        """Setup anti-detection measures."""
        await self._context.add_init_script('''
            // 移除 webdriver 标记。
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            
            // 模拟浏览器插件。
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });

            // 部分 venue 页面按 document 可见性决定是否订阅 WS。
            // Data/Exec 共用浏览器时页面可能在后台,因此固定为可见态。
            try {
                Object.defineProperty(document, 'hidden', {
                    get: () => false,
                    configurable: true
                });
            } catch (e) {}
            try {
                Object.defineProperty(document, 'visibilityState', {
                    get: () => 'visible',
                    configurable: true
                });
            } catch (e) {}
            try {
                document.hasFocus = () => true;
            } catch (e) {}

            const OriginalIntersectionObserver = window.IntersectionObserver;
            if (OriginalIntersectionObserver) {
                window.IntersectionObserver = function(callback, options) {
                    const observer = new OriginalIntersectionObserver((entries, obs) => {
                        callback(entries.map(entry => ({
                            boundingClientRect: entry.boundingClientRect,
                            intersectionRatio: 1.0,
                            intersectionRect: entry.boundingClientRect,
                            isIntersecting: true,
                            isVisible: true,
                            rootBounds: entry.rootBounds,
                            target: entry.target,
                            time: entry.time
                        })), obs);
                    }, options);
                    const originalObserve = observer.observe.bind(observer);
                    observer.observe = function(target) {
                        originalObserve(target);
                        setTimeout(() => {
                            const rect = target.getBoundingClientRect();
                            callback([{
                                boundingClientRect: rect,
                                intersectionRatio: 1.0,
                                intersectionRect: rect,
                                isIntersecting: true,
                                isVisible: true,
                                rootBounds: null,
                                target,
                                time: performance.now()
                            }], observer);
                        }, 10);
                    };
                    return observer;
                };
                window.IntersectionObserver.prototype = OriginalIntersectionObserver.prototype;
            }
        ''')
    
    async def create_page(self, name: str) -> Page:
        """
        Create a new page.
        
        Parameters
        ----------
        name : str
            Page identifier
            
        Returns
        -------
        Page
            The created page
        """
        if name in self._pages:
            return self._pages[name]
        
        page = await self._context.new_page()
        page.set_default_timeout(30000)
        
        self._pages[name] = page
        self._log.info(f'📄 Created page: {name}')
        
        return page
    
    async def get_page(self, name: str) -> Optional[Page]:
        """Get an existing page."""
        return self._pages.get(name)
    
    async def close_page(self, name: str) -> None:
        """Close a page."""
        page = self._pages.pop(name, None)
        if page:
            await page.close()
            self._log.info(f'🗑️  Closed page: {name}')
    
    async def screenshot(self, page_name: str, path: str) -> None:
        """
        Take a screenshot for debugging.
        
        Parameters
        ----------
        page_name : str
            Page identifier
        path : str
            Path to save screenshot
        """
        page = self._pages.get(page_name)
        if page:
            await page.screenshot(path=path)
            self._log.info(f'📸 Screenshot saved: {path}')
    
    async def close(self) -> None:
        """Close the browser."""
        self._log.info('🛑 Closing browser...')
        
        # Close all pages
        for name in list(self._pages.keys()):
            await self.close_page(name)
        
        # Close context
        if self._context:
            await self._context.close()
        
        # Close browser
        if self._browser:
            await self._browser.close()
        
        # Stop Playwright
        if self._playwright:
            await self._playwright.stop()
        
        self._log.info('✅ Browser closed')
