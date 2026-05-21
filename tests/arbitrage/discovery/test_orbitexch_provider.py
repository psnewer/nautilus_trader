"""
OE InstrumentProvider 测试(用例见 README.md)。

迁移决定: OE 没有上游适配器,自写 OrbitExchInstrumentProvider。本测试验证:
- BettingInstrument 类型 + InstrumentId 命名可逆
- info dict 必含 §6.4 锁定的 6 个统一 key(供异构归一)
- Browser 通过 manager 的 page name "discovery" 拿专属 page
- 抓取失败不污染 Cache 也不破坏 BrowserManager 状态

对应章节: refactor.md §5.1.2, §6.2, §6.4
"""

import pytest


@pytest.mark.skip(reason="not implemented; impl pending Step 1 execution")
def test_oe_provider_cold_start():
    """
    discovery-1.4: OE Provider 冷启动加载

    验证 cache.instruments(venue=ORBITEXCH) 非空且 BettingInstrument 类型,
    InstrumentId 格式 = {market_id}-{selection_id}.ORBITEXCH 可被 helper 反解。
    """


@pytest.mark.skip(reason="not implemented")
def test_oe_provider_info_dict_unified_keys():
    """
    discovery-1.5: OE Provider info dict 必含 6 个统一 key (Q9)

    验证 instrument.info 含 sport / competition / home_team / away_team /
    start_ts / selection_role 全部 6 个 key 且类型正确。
    这是 MarketMatchingActor 跨 venue 归一的依赖,Step 1 OE 实施时必须填齐。
    """


@pytest.mark.skip(reason="not implemented")
def test_pm_oe_share_match_keys():
    """
    discovery-1.6: PM 与 OE Provider 共享匹配字段

    验证 PM BinaryOption 与 OE BettingInstrument 的 info dict 都含相同的
    6 个 key,语义一致。MatchEngine 不依赖 isinstance 区分类型。
    """


@pytest.mark.skip(reason="not implemented")
def test_oe_provider_uses_discovery_page_name():
    """
    discovery-1.4 (子) : OE Provider 用 page name "discovery"

    验证 provider 调 browser_manager.create_page("discovery"),
    不是 "data" / "execution"(三方共享 BrowserContext,各自专属 page)。
    """


@pytest.mark.skip(reason="not implemented")
def test_oe_provider_scrape_failure_preserves_cache():
    """
    discovery-1.7: OE Provider 抓取失败处理

    模拟 Playwright 加载超时 / 元素找不到。验证:
    - 不抛异常
    - Cache 不被清空
    - 不调 manager.close()(provider 不拥有 manager 生命周期)
    """
