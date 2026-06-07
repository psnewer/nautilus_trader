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
    health_interval_secs : float, default 15.0
        健康检查 loop 节奏(§6.8.3/§6.8.4.5);每 tick 评估 competition 页 staleness。
        应 ≤ 旧 staleness 检查间隔(30s 量级,refactor.md 行 1245),否则 stale 发现太慢。
    staleness_timeout_secs : float, default 30.0
        competition 页赔率冻结阈值(§6.8.3 时间维度);`now-last_update_ns>阈值` → 页面 reload。
    health_check_exec_reload_enabled : bool, default False
        §6.8.3 状态维度(Phase 2):`leg_settled` 有未结算腿时是否 reload execution 页。
        **默认 False(安全闸)**:reload 已登录交易页的弹窗/会话行为未经真单 live 验,
        live 验通过前不自动 reload 交易页;live 验时显式置 True。
    """

    username: str
    password: str
    base_url: str = 'https://orbitexch.com'
    headless: bool = True
    browser_type: str = 'chromium'
    user_data_dir: Optional[str] = None
    page_timeout: int = 120000
    scrape_interval_ms: int = 1000
    update_instruments_interval_mins: Optional[int] = 60
    health_interval_secs: float = 15.0
    staleness_timeout_secs: float = 30.0
    health_check_exec_reload_enabled: bool = False


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
    page_timeout: int = 120000
    max_bet_amount: float = 10000.0
    confirm_bet: bool = True
