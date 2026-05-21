"""
InstrumentsRefreshed 自定义 Data 类型与 MessageBus 契约测试。

InstrumentsRefreshed 是 Refresher → MatchingActor 之间的批次完成事件。
通过 NT @customdataclass 注册成 Data 子类,走 MessageBus 标准 publish/subscribe。

对应章节: refactor.md §5.2.2, §5.3, §6.4
"""

import pytest


@pytest.mark.skip(reason="not implemented; impl pending Step 2")
def test_instruments_refreshed_data_type_registration():
    """
    discovery-3.1: InstrumentsRefreshed Data 类型注册

    验证类通过 @customdataclass 注册,字段完整(venue / count / ts_event / ts_init),
    DataType(InstrumentsRefreshed) 可用于 subscribe_data。
    """


@pytest.mark.skip(reason="not implemented")
def test_instruments_refreshed_msgbus_topic_routing():
    """
    discovery-3.2: MessageBus topic 契约

    Refresher publish 一条 InstrumentsRefreshed,订阅 DataType(InstrumentsRefreshed)
    的 actor 能收到事件。具体 topic 路径与 NT 标准对齐(Step 2 启动时确认)。
    """
