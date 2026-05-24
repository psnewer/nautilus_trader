"""
InstrumentsRefreshed 自定义 Data 类型与 MessageBus 契约测试。

通过 NT @customdataclass 注册成 Data 子类,走 MessageBus 标准 publish/subscribe。
对应章节: refactor.md §5.2.2, §5.3, §6.4
"""

from nautilus_trader.core import Data
from nautilus_trader.model.data import DataType

from src.arbitrage.discovery.events import InstrumentsRefreshed


def test_instruments_refreshed_is_data_subclass():
    """discovery-3.1: InstrumentsRefreshed Data 类型注册;DataType(...) 可用于 subscribe_data。"""
    assert issubclass(InstrumentsRefreshed, Data)
    dt = DataType(InstrumentsRefreshed)
    assert dt.type is InstrumentsRefreshed


def test_instruments_refreshed_fields_and_timestamps():
    """discovery-3.2: 字段完整(venue/count/ts_event/ts_init),@customdataclass 注入时间戳字段。"""
    e = InstrumentsRefreshed(ts_event=100, ts_init=200, venue="POLYMARKET", count=42)
    assert e.venue == "POLYMARKET"
    assert e.count == 42
    assert e.ts_event == 100
    assert e.ts_init == 200


def test_instruments_refreshed_dict_roundtrip():
    """discovery-3.3: to_dict/from_dict 可逆(消费端解码契约稳定)。"""
    src = InstrumentsRefreshed(ts_event=10, ts_init=20, venue="ORBITEXCH", count=3)
    out = src.to_dict()
    assert out == {"venue": "ORBITEXCH", "count": 3, "ts_event": 10, "ts_init": 20}
    rebuilt = InstrumentsRefreshed.from_dict(out)
    assert rebuilt.venue == src.venue and rebuilt.count == src.count
    assert rebuilt.ts_event == src.ts_event and rebuilt.ts_init == src.ts_init
