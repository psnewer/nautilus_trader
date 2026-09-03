"""OneSideRecoveryCheck:单冲机会触发后按最大在场 share 补救。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from nautilus_trader.model.enums import PositionSide
from src.arbitrage.common.venues import ORBITEXCH
from src.arbitrage.common.venues import probability_from_price
from src.arbitrage.strategy.checks.one_side_recovery import OneSideRecoveryCheck
from tests.arbitrage.strategy._live_state import live_context


class _Qty:
    def __init__(self, value):
        self._value = value

    def as_double(self):
        return self._value


def _book(ask):
    book = MagicMock()
    book.best_ask_price = MagicMock(return_value=ask)
    return book


def _position(iid, qty, price=0.50):
    return SimpleNamespace(
        instrument_id=iid,
        quantity=_Qty(qty),
        avg_px_open=price,
        side=PositionSide.LONG,
    )


def _ctx(*, books, infos, positions):
    return live_context(
        books=books,
        infos=infos,
        positions=positions,
        instrument_ids=list(infos),
        strategy_defaults={"share": 10.0},
    )


def test_hit_uses_current_max_position_as_recovery_target_without_candidate_leak():
    books = {
        "H.POLYMARKET": _book(0.45),
        "A.POLYMARKET": _book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[
            _position("H.POLYMARKET", 5.0),
            _position("A.POLYMARKET", 2.0),
        ],
    )

    assert OneSideRecoveryCheck(min_rate=0.10).passes(ctx) is True

    assert ctx.scratch["mean_rebate_recovery"]["target_share"] == 5.0
    assert ctx.scratch["legs"][0]["instrument_id"] == "A.POLYMARKET"
    assert ctx.scratch["legs"][0]["qty"] == 3.0
    assert ctx.scratch["one_side_recovery"]["min_rate"] == 0.10
    assert "candidates" not in ctx.scratch
    assert "one_side_rebate" not in ctx.scratch


def test_miss_when_one_side_rebate_is_below_min_rate():
    books = {
        "H.POLYMARKET": _book(0.50),
        "A.POLYMARKET": _book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position("H.POLYMARKET", 5.0)],
    )

    assert OneSideRecoveryCheck(min_rate=0.01, force=True).passes(ctx) is False
    assert ctx.scratch == {}


def test_less_true_hits_when_one_side_rebate_is_below_min_rate():
    books = {
        "H.POLYMARKET": _book(0.50),
        "A.POLYMARKET": _book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position("H.POLYMARKET", 5.0)],
    )

    assert OneSideRecoveryCheck(min_rate=0.01, force=True, less=True).passes(ctx) is True
    assert ctx.scratch["one_side_recovery"] == {
        "min_rate": 0.01,
        "candidate_count": 2,
    }


def test_less_true_uses_strict_comparison_at_equal_min_rate():
    books = {
        "H.POLYMARKET": _book(0.50),
        "A.POLYMARKET": _book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position("H.POLYMARKET", 5.0)],
    )

    assert OneSideRecoveryCheck(min_rate=0.0, force=True, less=True).passes(ctx) is False
    assert ctx.scratch == {}


def test_less_true_hits_when_any_candidate_is_below_min_rate():
    books = {
        "H.POLYMARKET": _book(0.45),
        "A.POLYMARKET": _book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position("H.POLYMARKET", 5.0)],
    )

    assert OneSideRecoveryCheck(min_rate=0.105, force=True, less=True).passes(ctx) is True
    assert ctx.scratch["one_side_recovery"]["candidate_count"] == 1


def test_less_false_explicitly_keeps_default_comparison():
    books = {
        "H.POLYMARKET": _book(0.45),
        "A.POLYMARKET": _book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position("H.POLYMARKET", 5.0)],
    )

    assert OneSideRecoveryCheck(min_rate=0.10, force=True, less=False).passes(ctx) is True
    assert ctx.scratch["one_side_recovery"] == {
        "min_rate": 0.10,
        "candidate_count": 2,
    }


def test_less_must_be_boolean():
    try:
        OneSideRecoveryCheck(less="true")
    except ValueError as exc:
        assert str(exc) == "one_side_recovery: less must be a boolean"
    else:
        raise AssertionError("expected invalid less to fail")


def test_force_only_bypasses_recovery_rebate_gates():
    books = {
        "H.POLYMARKET": _book(0.45),
        "A.POLYMARKET": _book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }

    normal = _ctx(books=books, infos=infos, positions=[_position("H.POLYMARKET", 5.0)])
    forced = _ctx(books=books, infos=infos, positions=[_position("H.POLYMARKET", 5.0)])

    assert OneSideRecoveryCheck(
        min_rate=0.10,
        min_repaired_rebate=0.10,
    ).passes(normal) is False
    assert OneSideRecoveryCheck(
        min_rate=0.10,
        min_repaired_rebate=0.10,
        force=True,
    ).passes(forced) is True
    assert forced.scratch["mean_rebate_recovery"]["force"] is True


def test_recovery_leg_uses_default_cross_venue_best_price():
    books = {
        "H.POLYMARKET": _book(0.45),
        "A.POLYMARKET": _book(0.55),
        "A.ORBITEXCH": _book(probability_from_price(ORBITEXCH, 2.0)),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
        "A.ORBITEXCH": {"selection_role": "away"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position("H.POLYMARKET", 5.0)],
    )

    assert OneSideRecoveryCheck(
        min_rate=0.10,
        force=True,
    ).passes(ctx) is True
    assert ctx.scratch["legs"][0]["instrument_id"] == "A.ORBITEXCH"
