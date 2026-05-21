"""
PM InstrumentProvider 测试(用例见 README.md)。

迁移决定: PM 使用上游 NT 的 PolymarketInstrumentProvider,本测试验证上游版本
满足我们的需求(BinaryOption 字段完整、InstrumentId 命名可逆、API 失败不污染 Cache)。

对应章节: refactor.md §5.1.1, §6.5
"""

import pytest


@pytest.mark.skip(reason="not implemented; impl pending Step 1 execution")
def test_pm_provider_cold_start():
    """
    discovery-1.1: PM Provider 冷启动加载

    验证 cache.instruments(venue=POLYMARKET) 非空且 BinaryOption 类型,
    InstrumentId 格式可被 get_polymarket_condition_id / get_polymarket_token_id 反解。
    """


@pytest.mark.skip(reason="not implemented")
def test_pm_provider_field_completeness():
    """
    discovery-1.2: PM Provider 字段完整度

    任取一个 BinaryOption,验证 price_increment / size_increment /
    min_quantity / max_quantity / taker_fee / maker_fee 字段非空,
    fee_schedule 富化生效(taker_fee 不为 0)。
    """


@pytest.mark.skip(reason="not implemented")
def test_pm_provider_api_failure_preserves_cache():
    """
    discovery-1.3: PM Provider API 失败处理

    模拟 Gamma API 5xx,验证 load_all_async 不抛异常 + Cache 保持上次成功快照。
    """
