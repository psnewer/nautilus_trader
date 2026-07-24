# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  you may not use this file except in compliance with the License.
# -------------------------------------------------------------------------------------------------

"""SharpExch adapter configuration."""

from typing import Optional

from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.config import LiveExecClientConfig


class SharpExchDataClientConfig(LiveDataClientConfig, frozen=True, kw_only=True):
    """SharpExch data client 配置。

    DataClient 不登录 SE;discovery 等共享 BrowserContext 的 CSRF 后请求 `sport/details`,
    再按 competition 页订阅 WS。
    """

    username: str
    password: str
    base_url: str = "https://portal.sharpxch.com"
    login_url: str = "https://sharpxch.com/player/"
    headless: bool = True
    browser_type: str = "chromium"
    user_data_dir: Optional[str] = None
    # #276:显式代理或直连(浏览器 launch;None → --no-proxy-server)
    proxy_url: str | None = None
    page_timeout: int = 120000
    scrape_interval_ms: int = 1000
    update_instruments_interval_mins: Optional[int] = 60
    staleness_timeout_secs: float = 300.0


class SharpExchExecClientConfig(LiveExecClientConfig, frozen=True, kw_only=True):
    """SharpExch execution client 配置。"""

    username: str
    password: str
    base_url: str = "https://portal.sharpxch.com"
    login_url: str = "https://sharpxch.com/player/"
    headless: bool = True
    browser_type: str = "chromium"
    user_data_dir: Optional[str] = None
    # #276:显式代理或直连(浏览器 launch;None → --no-proxy-server)
    proxy_url: str | None = None
    page_timeout: int = 120000
    cloudflare_timeout: int = 120000
    max_bet_amount: float = 10000.0
    confirm_bet: bool = True
