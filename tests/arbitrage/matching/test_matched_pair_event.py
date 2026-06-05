"""MatchedPair Data 类型与 MessageBus 契约。"""

from nautilus_trader.core import Data
from nautilus_trader.model.data import DataType

from src.arbitrage.matching.events import MatchedPair


def test_matched_pair_is_data_subclass():
    """matching-3.event.1: MatchedPair 注册为 Data;DataType 可用于 subscribe_data。"""
    assert issubclass(MatchedPair, Data)
    dt = DataType(MatchedPair)
    assert dt.type is MatchedPair


def test_matched_pair_fields_intact():
    """matching-3.event.2: 字段完整 + ts_event/ts_init 自动注入。"""
    e = MatchedPair(
        ts_event=10, ts_init=20,
        pair_id="EPL|A|B", sport="Soccer", competition="EPL",
        pm_instrument_ids=["A_h.PM", "A_a.PM"],
        oe_instrument_ids=["X_h.OE", "X_a.OE"],
        confidence=0.85,
    )
    assert e.pair_id == "EPL|A|B"
    assert e.competition == "EPL"             # league,非 pair_id(#34 强调)
    assert e.pm_instrument_ids == ["A_h.PM", "A_a.PM"]
    assert e.oe_instrument_ids == ["X_h.OE", "X_a.OE"]
    assert e.confidence == 0.85
    assert e.ts_event == 10 and e.ts_init == 20


def test_matched_pair_dict_roundtrip():
    """matching-3.event.3: to_dict/from_dict 可逆(消费端解码契约稳定)。"""
    src = MatchedPair(
        ts_event=1, ts_init=2,
        pair_id="p", sport="s", competition="c",
        pm_instrument_ids=["a"], oe_instrument_ids=["b"], confidence=1.0,
    )
    out = src.to_dict()
    rebuilt = MatchedPair.from_dict(out)
    assert rebuilt.pair_id == src.pair_id and rebuilt.confidence == src.confidence
    assert rebuilt.pm_instrument_ids == src.pm_instrument_ids
