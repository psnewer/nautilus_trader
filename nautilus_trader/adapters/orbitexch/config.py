# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
# -------------------------------------------------------------------------------------------------

"""OrbitExch adapter configuration."""

from typing import Optional

from nautilus_trader.config import LiveDataClientConfig, LiveExecClientConfig


class OrbitExchDataClientConfig(LiveDataClientConfig, frozen=True, kw_only=True):
    """
    Configuration for OrbitExch data client.
    
    Parameters
    ----------
    username : str
        OrbitExch account username
    password : str
        OrbitExch account password
    base_url : str, default 'https://orbitexch.com'
        Base URL for OrbitExch website
    headless : bool, default True
        Run browser in headless mode
    browser_type : str, default 'chromium'
        Browser type: 'chromium', 'firefox', or 'webkit'
    user_data_dir : str, optional
        Directory to persist browser session (for login persistence)
    page_timeout : int, default 30000
        Page load timeout in milliseconds
    scrape_interval_ms : int, default 1000
        Odds scraping interval in milliseconds
    """
    
    username: str
    password: str
    base_url: str = 'https://orbitexch.com'
    headless: bool = True
    browser_type: str = 'chromium'
    user_data_dir: Optional[str] = None
    page_timeout: int = 30000
    scrape_interval_ms: int = 1000


class OrbitExchExecClientConfig(LiveExecClientConfig, frozen=True, kw_only=True):
    """
    Configuration for OrbitExch execution client.
    
    Parameters
    ----------
    username : str
        OrbitExch account username
    password : str
        OrbitExch account password
    base_url : str, default 'https://orbitexch.com'
        Base URL for OrbitExch website
    headless : bool, default True
        Run browser in headless mode
    browser_type : str, default 'chromium'
        Browser type: 'chromium', 'firefox', or 'webkit'
    user_data_dir : str, optional
        Directory to persist browser session
    page_timeout : int, default 30000
        Page operation timeout in milliseconds
    max_bet_amount : float, default 10000.0
        Maximum bet amount (risk control)
    confirm_bet : bool, default True
        Require confirmation before placing bets
    """
    
    username: str
    password: str
    base_url: str = 'https://orbitexch.com'
    headless: bool = True
    browser_type: str = 'chromium'
    user_data_dir: Optional[str] = None
    page_timeout: int = 30000
    max_bet_amount: float = 10000.0
    confirm_bet: bool = True
