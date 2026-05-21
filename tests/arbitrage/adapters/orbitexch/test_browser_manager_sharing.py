"""
PlaywrightBrowserManager 在三方组件间的共享(Q2 验收)。

锁定决定:
- 沿用现有 nautilus_trader/adapters/orbitexch/browser_manager.py:PlaywrightBrowserManager
- 所有权从 DataClient 抽到 NT factory 层(get_orbitexch_browser_manager 单例)
- 三方共享 BrowserContext = 共享登录态
- 各自专属 page: "discovery" / "data" / "execution"

对应章节: refactor.md §6.2
"""

import pytest


@pytest.mark.skip(reason="not implemented; pending Step 1 factory wiring")
def test_three_components_share_single_manager():
    """oe-adapter-2.1: 三方组件共享同一 PlaywrightBrowserManager 实例"""


@pytest.mark.skip(reason="not implemented")
def test_components_dont_own_manager_lifecycle():
    """
    oe-adapter-2.2: 组件不持有 manager 生命周期

    Provider / DataClient / ExecutionClient 的 _disconnect 不调用
    manager.start() 或 manager.close(),只 close 自己的 page。
    """


@pytest.mark.skip(reason="not implemented")
def test_user_data_dir_persists_login():
    """
    oe-adapter-2.3: user_data_dir 持久化登录态

    配置 user_data_dir,登录,关闭进程,重启。验证重启后无需重新登录。
    """


@pytest.mark.skip(reason="not implemented")
def test_each_component_uses_distinct_page_name():
    """
    oe-adapter-2.x: 三方使用约定的 page name

    Provider 用 "discovery",DataClient 用 "data",ExecutionClient 用 "execution"。
    通过 manager._pages dict 查证。
    """
