"""
上游 PM 适配器在套利系统配置下的集成验证(用例见 README.md)。

不测试上游本身(NT 框架自己有测试),只验证上游版本在我们的配置 / 用法下满足
套利系统需求,且不需要修改上游代码。

对应章节: refactor.md §6.5
"""

import pytest


@pytest.mark.skip(reason="not implemented; pending Step 1")
def test_upstream_provider_loads_with_arbitrage_config():
    """pm-adapter-1.1: 上游 InstrumentProvider 在套利系统配置下加载"""


@pytest.mark.skip(reason="not implemented; pending Step 1; CRITICAL for §6.4")
def test_upstream_binary_option_info_dict_fields():
    """
    pm-adapter-1.2: BinaryOption.info 是否含 §6.4 6 个统一 key

    输出影响 §6.4 实现路径(直接可用 vs 需子类化 Provider 补齐 info)。
    Step 1 实施时第一个跑的用例。
    """


@pytest.mark.skip(reason="not implemented; pending Step 2")
def test_upstream_data_client_outputs_orderbookdelta():
    """pm-adapter-2.1: 上游 DataClient 输出 NT 标准 OrderBookDelta"""


@pytest.mark.skip(reason="not implemented; pending Step 5")
def test_upstream_exec_client_submit_with_event_writeback():
    """
    pm-adapter-5.1: 上游 ExecutionClient 下单 + 事件回写

    需要测试网账户。验证 bug_polymarket_order_version_mismatch 不复现。
    """


@pytest.mark.skip(reason="not implemented; pending Step 5")
def test_upstream_exec_client_cancel():
    """pm-adapter-5.2: 撤单接口"""


@pytest.mark.skip(reason="not implemented; pending Step 5")
def test_upstream_reconciliation_on_restart():
    """pm-adapter-5.3: 重启后通过 generate_*_status_reports 与 venue 对账"""
