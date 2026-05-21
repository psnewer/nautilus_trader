"""
OrbitExchInstrumentProvider 测试。

详细用例与 discovery/test_orbitexch_provider.py 重叠;此处的测试聚焦
"OE 适配器层独有"的 venue-specific 行为(scrape 解析 / 站点结构变化容错 / 登录态依赖等)。

对应章节: refactor.md §5.1.2
"""

import pytest


@pytest.mark.skip(reason="not implemented; pending Step 1")
def test_provider_parses_in_play_listing():
    """
    oe-adapter-1.x: OE in-play 赛事列表页解析

    输入: 一个真实抓取的 HTML 快照(放 _helpers/ 目录下)
    期望: 正确解析出 events / markets / selections 列表
    验收: 字段完整,数量与人工核对一致
    """


@pytest.mark.skip(reason="not implemented")
def test_provider_handles_site_layout_change():
    """
    oe-adapter-1.x: 站点结构小变更时容错

    输入: 一个 selector 失效的 HTML 快照
    期望: 不抛异常,日志告警,空结果(不污染 Cache)
    """


@pytest.mark.skip(reason="not implemented")
def test_provider_requires_login():
    """
    oe-adapter-1.x: 未登录时拒绝加载

    前置: 清空 cookie
    输入: 调 load_all_async
    期望: 检测到未登录,拒绝加载并日志告警(不抓取登录墙后的占位页)
    """
