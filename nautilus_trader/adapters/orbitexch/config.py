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
    page_timeout : int, default 120000
        Page load timeout in milliseconds(OE 页面默认等 120s;#68)
    scrape_interval_ms : int, default 1000
        Odds scraping interval in milliseconds
    update_instruments_interval_mins : int or None, default 60
        Periodic instrument re-discovery interval (mins); None disables. #58(slice A):
        DataClient 拥有周期发现(原生 `_update_instruments`),替代退役的 InstrumentRefresher。
    staleness_timeout_secs : float, default 300.0
        #109:competition 页 WS handler 内部 liveness timeout —— 超 此时间无任何帧(含 SockJS 心跳 ~25s)
        → 视为 WS 死 → `on_disconnect` → reload。(旧 `health_interval_secs` 随 HealthCheckLoop #109 退役。)
        默认 300.0 对齐 SE 与 `config/schema.py` 的 arb 默认:#110 实测空闲盘口心跳 median 25.0s /
        **max 35.4s**,旧默认 30.0 低于实测最大间隔 → 安静市场会误判 disconnect、reload 空转。
    """

    username: str
    password: str
    base_url: str = 'https://orbitexch.com'
    headless: bool = True
    browser_type: str = 'chromium'
    user_data_dir: Optional[str] = None
    # #276:显式代理或直连(浏览器 launch;None → --no-proxy-server)
    proxy_url: str | None = None
    page_timeout: int = 120000
    scrape_interval_ms: int = 1000
    update_instruments_interval_mins: Optional[int] = 60
    staleness_timeout_secs: float = 300.0


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
    page_timeout : int, default 120000
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
    # #276:显式代理或直连(浏览器 launch;None → --no-proxy-server)
    proxy_url: str | None = None
    page_timeout: int = 120000
    max_bet_amount: float = 10000.0
    confirm_bet: bool = True
