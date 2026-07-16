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
        confidence=0.85,
        anchor_instrument_ids=["anchor.PMSPORTS"],
        tradable_instrument_ids=["A_h.PM", "A_a.PM", "X_h.OE", "X_a.OE"],
        venue_instrument_ids={"PM": ["A_h.PM", "A_a.PM"], "OE": ["X_h.OE", "X_a.OE"]},
        order_books_managed=True,
    )
    assert e.pair_id == "EPL|A|B"
    assert e.competition == "EPL"             # league,非 pair_id(#34 强调)
    assert e.anchor_instrument_ids == ["anchor.PMSPORTS"]
    assert e.tradable_instrument_ids == ["A_h.PM", "A_a.PM", "X_h.OE", "X_a.OE"]
    assert e.venue_instrument_ids["PM"] == ["A_h.PM", "A_a.PM"]
    assert e.confidence == 0.85
    assert e.order_books_managed is True
    assert e.ts_event == 10 and e.ts_init == 20


def test_matched_pair_has_no_legacy_projection_fields():
    """matching-3.event.2b:keyed venue map 是主通路,事件层不再携带旧 PM/OE 投影。"""
    e = MatchedPair(
        ts_event=10,
        ts_init=20,
        pair_id="EPL|A|B",
        sport="Soccer",
        competition="EPL",
        confidence=0.85,
        anchor_instrument_ids=["anchor.PMSPORTS"],
        tradable_instrument_ids=[
            "A_h.POLYMARKET",
            "A_a.POLYMARKET",
            "X_h.ORBITEXCH",
            "X_a.ORBITEXCH",
            "S_h.SHARPEXCH",
        ],
        venue_instrument_ids={
            "POLYMARKET": ["A_h.POLYMARKET", "A_a.POLYMARKET"],
            "ORBITEXCH": ["X_h.ORBITEXCH", "X_a.ORBITEXCH"],
            "SHARPEXCH": ["S_h.SHARPEXCH"],
        },
    )

    assert not hasattr(e, "pm_instrument_ids")
    assert not hasattr(e, "oe_instrument_ids")
    assert e.venue_instrument_ids["SHARPEXCH"] == ["S_h.SHARPEXCH"]


def test_matched_pair_dict_roundtrip():
    """matching-3.event.3: to_dict/from_dict 可逆(消费端解码契约稳定)。"""
    src = MatchedPair(
        ts_event=1, ts_init=2,
        pair_id="p", sport="s", competition="c",
        confidence=1.0,
        anchor_instrument_ids=["anchor.PMSPORTS"],
        tradable_instrument_ids=["a", "b"],
        venue_instrument_ids={"POLYMARKET": ["a"], "ORBITEXCH": ["b"]},
        order_books_managed=True,
    )
    out = src.to_dict()
    rebuilt = MatchedPair.from_dict(out)
    assert rebuilt.pair_id == src.pair_id and rebuilt.confidence == src.confidence
    assert rebuilt.anchor_instrument_ids == src.anchor_instrument_ids
    assert rebuilt.tradable_instrument_ids == src.tradable_instrument_ids
    assert rebuilt.venue_instrument_ids == src.venue_instrument_ids
    assert rebuilt.order_books_managed is True


def test_matched_pair_from_legacy_dict_ignores_old_projection_fields():
    """matching-3.event.4:旧 pm/oe payload 被忽略,避免掩盖 keyed map 缺失。"""
    rebuilt = MatchedPair.from_dict({
        "ts_event": 1,
        "ts_init": 2,
        "pair_id": "p",
        "sport": "s",
        "competition": "c",
        "pm_instrument_ids": ["pm-home.POLYMARKET"],
        "oe_instrument_ids": ["oe-home.ORBITEXCH", "se-home.SHARPEXCH"],
        "confidence": 0.9,
    })

    assert rebuilt.tradable_instrument_ids == []
    assert rebuilt.venue_instrument_ids == {}
    assert not hasattr(rebuilt, "pm_instrument_ids")
    assert not hasattr(rebuilt, "oe_instrument_ids")


def test_matched_pair_arrow_roundtrip_preserves_venue_map():
    """matching-3.event.6:Arrow schema 支持 venue->legs map 字段。"""
    src = MatchedPair(
        ts_event=1,
        ts_init=2,
        pair_id="p",
        sport="s",
        competition="c",
        confidence=0.9,
        anchor_instrument_ids=["anchor.PMSPORTS"],
        tradable_instrument_ids=["pm-home.POLYMARKET", "oe-home.ORBITEXCH"],
        venue_instrument_ids={
            "POLYMARKET": ["pm-home.POLYMARKET"],
            "ORBITEXCH": ["oe-home.ORBITEXCH"],
        },
    )

    rebuilt = MatchedPair.from_arrow(src.to_arrow())[0]

    assert rebuilt.venue_instrument_ids == src.venue_instrument_ids
    assert rebuilt.anchor_instrument_ids == src.anchor_instrument_ids
