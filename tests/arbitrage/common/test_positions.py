"""Pair position baseline 摘要契约。"""

from types import SimpleNamespace

from src.arbitrage.common.positions import pair_positions_digest
from src.arbitrage.common.positions import positions_digest


def _position(
    position_id,
    *,
    side="LONG",
    quantity="10",
    avg_px_open=0.4,
    avg_px_close=0.0,
    realized_pnl="1.0 USD",
    event_count=1,
    ts_last=1,
):
    return SimpleNamespace(
        id=position_id,
        account_id="POLYMARKET-001",
        instrument_id="A.POLYMARKET",
        strategy_id="STRATEGY-001",
        side=side,
        quantity=quantity,
        avg_px_open=avg_px_open,
        avg_px_close=avg_px_close,
        realized_pnl=realized_pnl,
        event_count=lambda: event_count,
        ts_last=ts_last,
    )


class _Cache:
    def __init__(self, positions):
        self.items = list(positions)

    def positions(self, *, instrument_id):
        return [
            position
            for position in self.items
            if str(position.instrument_id) == str(instrument_id)
        ]


def test_digest_is_stable_across_position_object_identity_and_cache_order():
    first = _Cache([_position("a"), _position("b")])
    second = _Cache([_position("b"), _position("a")])

    assert pair_positions_digest(first, ["A.POLYMARKET"]) == pair_positions_digest(
        second,
        ["A.POLYMARKET"],
    )


def test_digest_changes_when_position_economics_change():
    baseline = pair_positions_digest(_Cache([_position("a")]), ["A.POLYMARKET"])

    changes = [
        _position("a", side="SHORT"),
        _position("a", quantity="8"),
        _position("a", avg_px_open=0.5),
        _position("a", avg_px_close=0.5),
        _position("a", realized_pnl="2.0 USD"),
    ]
    for changed in changes:
        assert pair_positions_digest(_Cache([changed]), ["A.POLYMARKET"]) != baseline


def test_digest_changes_when_position_disappears():
    baseline = pair_positions_digest(_Cache([_position("a")]), ["A.POLYMARKET"])

    assert pair_positions_digest(_Cache([]), ["A.POLYMARKET"]) != baseline


def test_positions_digest_detects_event_generation_change_without_economic_change():
    baseline = positions_digest([_position("a", event_count=1, ts_last=1)])

    assert positions_digest([_position("a", event_count=2, ts_last=2)]) != baseline
