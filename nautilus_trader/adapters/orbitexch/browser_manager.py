# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
# -------------------------------------------------------------------------------------------------

"""Playwright browser manager for OrbitExch."""

import asyncio
import logging
from typing import Dict, Optional

try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
except ImportError:
    raise ImportError(
        'Playwright is required for OrbitExch adapter. '
        'Install it with: pip install playwright && playwright install chromium'
    )


class PlaywrightBrowserManager:
    """
    Manages Playwright browser instances for OrbitExch automation.
    
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
        
        self._log = logging.getLogger(self.__class__.__name__)
    
    async def start(self) -> None:
        """Start the browser."""
        self._log.info('🚀 Starting Playwright browser...')
        
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
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            )
            self._browser = self._context.browser
        else:
            self._browser = await launcher.launch(
                headless=self.headless,
                args=launch_args,
            )
            self._context = await self._browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            )
        
        # Anti-detection
        await self._setup_stealth()
        
        self._log.info('✅ Browser started successfully')
    
    async def _setup_stealth(self) -> None:
        """Setup anti-detection measures."""
        await self._context.add_init_script('''
            // Remove webdriver flag
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            
            // Mock plugins
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
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
