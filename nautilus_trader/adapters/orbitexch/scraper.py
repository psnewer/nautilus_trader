# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
# -------------------------------------------------------------------------------------------------

"""OrbitExch page scraper."""

import asyncio
import logging
from typing import Optional, Dict, List, Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout


class OrbitExchScraper:
    """
    OrbitExch page scraper.
    
    Handles:
    - Login (with popup dismissal)
    - Event scraping
    - Odds scraping
    - Bet placement
    
    Parameters
    ----------
    page : Page
        Playwright page instance
    base_url : str
        Base URL for OrbitExch
    """
    
    def __init__(self, page: Page, base_url: str = 'https://orbitexch.com'):
        self.page = page
        self.base_url = base_url
        self._log = logging.getLogger(self.__class__.__name__)
    
    # ========== Login ==========
    
    async def login(self, username: str, password: str) -> bool:
        """
        Login to OrbitExch.
        
        Parameters
        ----------
        username : str
            Account username
        password : str
            Account password
            
        Returns
        -------
        bool
            True if login successful
        """
        self._log.info('🔐 Logging in to OrbitExch...')
        
        try:
            # Navigate to homepage
            await self.page.goto(self.base_url, wait_until='networkidle')
            
            # Wait for login form
            await self.page.wait_for_selector('input[name="username"]', timeout=10000)
            
            # Fill username
            await self.page.fill('input[name="username"]', username)
            self._log.debug(f'   Username filled: {username}')
            
            # Fill password
            await self.page.fill('input[name="password"]', password)
            self._log.debug('   Password filled')
            
            # Click login button
            await self.page.click('button[type="submit"]:has-text("Log In")')
            self._log.debug('   Login button clicked')
            
            # Wait for navigation
            try:
                await self.page.wait_for_url('**/customer/**', timeout=15000)
                self._log.info('✅ Login successful')
            except PlaywrightTimeout:
                # Check if login still succeeded despite timeout
                if 'customer' not in self.page.url:
                    self._log.error('❌ Login failed')
                    return False
                self._log.info('✅ Login successful (timeout but URL changed)')
            
            # Handle post-login popup
            await self._handle_post_login_popup()
            
            return True
        
        except Exception as e:
            self._log.error(f'❌ Login error: {e}')
            return False
    
    async def _handle_post_login_popup(self) -> None:
        """Handle popup that appears after login."""
        self._log.info('   Checking for post-login popup...')
        
        try:
            # 等待页面稳定
            await asyncio.sleep(2)
            
            # 使用经过验证的选择器
            ok_button = self.page.locator('xpath=//button[normalize-space()="OK"]')
            
            # 检查是否可见（增加超时时间到 5 秒）
            if await ok_button.is_visible(timeout=5000):
                self._log.info('   📋 Found popup, clicking OK...')
                await ok_button.click()
                await asyncio.sleep(1)
                self._log.info('   ✅ Popup dismissed')
            else:
                self._log.debug('   No popup found')
        
        except Exception as e:
            self._log.debug(f'   No popup (error: {e})')
    
    # ========== Events ==========
    
    async def scrape_inplay_events(self) -> List[Dict[str, Any]]:
        """
        Scrape in-play events.
        
        Returns
        -------
        List[Dict]
            List of events
        """
        self._log.info('📊 Scraping in-play events...')
        
        # Navigate to in-play page
        url = f'{self.base_url}/customer/inplay/highlights'
        await self.page.goto(url, wait_until='networkidle')
        
        # Wait for content
        await asyncio.sleep(2)
        
        # Extract events
        events = await self.page.evaluate('''() => {
            const events = [];
            
            // Find event containers
            const eventElements = document.querySelectorAll('[class*="event"], [class*="match"], [data-event-id]');
            
            eventElements.forEach((el, index) => {
                const eventName = el.querySelector('[class*="name"], [class*="title"]')?.textContent?.trim();
                const eventId = el.getAttribute('data-event-id') || el.getAttribute('id') || String(index);
                
                if (eventName) {
                    events.push({
                        event_id: eventId,
                        event_name: eventName,
                        sport: 'Unknown',
                        status: 'in-play',
                    });
                }
            });
            
            return events;
        }''')
        
        self._log.info(f'   Found {len(events)} events')
        return events
    
    async def scrape_market_odds(
        self,
        event_id: str,
        market_type: str = 'match_odds',
    ) -> Optional[Dict[str, Any]]:
        """
        Scrape market odds.
        
        Parameters
        ----------
        event_id : str
            Event identifier
        market_type : str
            Market type
            
        Returns
        -------
        Dict or None
            Market odds
        """
        self._log.info(f'📊 Scraping odds for event {event_id}')
        
        # Extract odds from current page
        odds = await self.page.evaluate('''(marketType) => {
            const market = {
                market_id: marketType,
                market_name: marketType,
                runners: [],
            };
            
            // Find runner elements
            const runnerElements = document.querySelectorAll('[class*="runner"], [class*="selection"]');
            
            runnerElements.forEach((runnerEl, index) => {
                const runnerName = runnerEl.querySelector('[class*="name"]')?.textContent?.trim();
                
                // Extract back/lay odds
                const backButton = runnerEl.querySelector('[class*="back"]');
                const layButton = runnerEl.querySelector('[class*="lay"]');
                
                const backPrice = parseFloat(backButton?.textContent?.match(/\\d+\\.\\d+/)?.[0] || '0');
                const layPrice = parseFloat(layButton?.textContent?.match(/\\d+\\.\\d+/)?.[0] || '0');
                
                if (runnerName) {
                    market.runners.push({
                        selection_id: String(index),
                        runner_name: runnerName,
                        back: { price: backPrice, size: 0 },
                        lay: { price: layPrice, size: 0 },
                    });
                }
            });
            
            return market;
        }''', market_type)
        
        return odds
    
    # ========== Betting ==========
    
    async def place_bet(
        self,
        selection_id: str,
        side: str,
        price: float,
        stake: float,
    ) -> Dict[str, Any]:
        """
        Place a bet.
        
        Parameters
        ----------
        selection_id : str
            Selection ID
        side : str
            'back' or 'lay'
        price : float
            Odds
        stake : float
            Stake amount
            
        Returns
        -------
        Dict
            Bet result
        """
        self._log.info(f'💰 Placing {side} bet: {stake} at {price}')
        
        try:
            # Click odds button (selector may need adjustment)
            button_selector = f'[data-selection="{selection_id}"] [class*="{side}"]'
            await self.page.click(button_selector)
            
            # Wait for bet slip
            await self.page.wait_for_selector('[class*="bet-slip"], [class*="betslip"]', timeout=5000)
            
            # Fill stake
            await self.page.fill('input[name*="stake" i]', str(stake))
            
            # Submit bet
            await self.page.click('button:has-text("Place Bet"), button:has-text("Confirm")')
            
            # Wait for result
            await asyncio.sleep(2)
            
            return {
                'success': True,
                'bet_id': 'unknown',
                'message': 'Bet placed',
            }
        
        except Exception as e:
            self._log.error(f'❌ Bet failed: {e}')
            return {
                'success': False,
                'bet_id': None,
                'message': str(e),
            }
    
    # ========== Account ==========
    
    async def get_balance(self) -> Dict[str, float]:
        """
        Get account balance.
        
        Returns
        -------
        Dict
            Balance information
        """
        self._log.info('💰 Getting balance...')
        
        balance_data = await self.page.evaluate('''() => {
            const balanceEl = document.querySelector('[class*="balance"], [class*="wallet"]');
            const balanceText = balanceEl?.textContent?.replace(/[^0-9.]/g, '') || '0';
            
            return {
                balance: parseFloat(balanceText) || 0,
                exposure: 0,
                available: parseFloat(balanceText) || 0,
            };
        }''')
        
        balance = balance_data.get('balance', 0)
        self._log.info(f'   Balance: {balance}')
        return balance_data
