"""
OrbitExchDataClient 测试(占位,详细用例待 Step 2 启动时展开)。

锁定决定(Step 2 实施前必须满足):
- 继承 NT LiveMarketDataClient(不是 LiveDataClient)
- 输出 NT 标准 OrderBookDelta(不是 QuoteTick)
- 通过 manager.get_page("data") 拿专属 page,不持有 manager 生命周期
- WS 帧拦截 + 解析 + 重连机制平移自现有 odds_client.py

对应章节: refactor.md §5.2.1, §6.2
"""

import pytest


@pytest.mark.skip(reason="not implemented; pending Step 2")
def test_data_client_inherits_live_market_data_client():
    """oe-adapter-2.x: 基类是 LiveMarketDataClient(注: 当前半成品 data.py 用了错的 LiveDataClient)"""


@pytest.mark.skip(reason="not implemented; pending Step 2")
def test_data_client_outputs_orderbookdelta():
    """oe-adapter-2.x: 输出 OrderBookDelta(注: 当前半成品输出 QuoteTick,需重写)"""


@pytest.mark.skip(reason="not implemented; pending Step 2")
def test_data_client_uses_data_page():
    """oe-adapter-2.x: 通过 manager.get_page('data') 拿 page,不调 start/close"""


@pytest.mark.skip(reason="not implemented; pending Step 2")
def test_data_client_subscription_dedup_via_data_engine():
    """oe-adapter-2.x: 多 actor 订阅同一 instrument,DataEngine 引用计数去重,WS 只订一次"""


@pytest.mark.skip(reason="not implemented; pending Step 2")
def test_data_client_reconnect_on_ws_drop():
    """oe-adapter-2.x: WS 断开后自动重连(Playwright page reload)+ 重新订阅"""
