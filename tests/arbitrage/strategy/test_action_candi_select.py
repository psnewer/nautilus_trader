"""CandiSelectAction:本树最小下注门控 → max leg share 选择。"""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd

from nautilus_trader.model.currencies import USD
from nautilus_trader.model.instruments import BettingInstrument
from nautilus_trader.model.instruments.betting import null_handicap
from nautilus_trader.model.objects import Money
from src.arbitrage.strategy.actions.candi_select import CandiSelectAction
from src.arbitrage.strategy.condition import EvalContext


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class _Cache:
    def __init__(self, instruments: dict):
        self._instruments = {str(key): value for key, value in instruments.items()}

    def instrument(self, instrument_id):
        return self._instruments.get(str(instrument_id))


def _pm_instrument(min_quantity=5.0, min_buy_notional=1.0):
    """PM 门槛 mock:float 字段走 object_value 退回口径,无 make_qty/notional_value。"""
    return SimpleNamespace(
        min_quantity=min_quantity,
        min_notional=None,
        size_increment=None,
        info={
            "min_buy_notional": min_buy_notional,
        },
    )


def _pm_leg(qty: float, share: float, price: float = 0.5):
    return {
        "instrument_id": "H.POLYMARKET",
        "venue": "POLYMARKET",
        "side": "BUY",
        "price": price,
        "qty": qty,
        "share_if_wins": share,
    }


def _oe_instrument(min_stake_usd: float):
    return BettingInstrument(
        venue_name="ORBITEXCH", betting_type="ODDS",
        competition_id=1, competition_name="EPL",
        event_country_code="", event_id=1, event_name="H v A",
        event_open_date=pd.Timestamp("2030-02-07 23:30:00+00:00"),
        event_type_id=1, event_type_name="Soccer",
        market_id="1-123", market_name="home",
        market_start_time=pd.Timestamp("2030-02-07 23:30:00+00:00"),
        market_type="MATCH_ODDS",
        selection_handicap=null_handicap(),
        selection_id=1, selection_name="home",
        currency="USD", price_precision=2, size_precision=2,
        min_notional=Money(Decimal(str(min_stake_usd)), USD),
        ts_event=0, ts_init=0,
        info={"sport": "Soccer", "selection_role": "home"},
    )


def test_selects_candidate_whose_largest_leg_share_is_largest():
    ctx = EvalContext(pair_id="p")
    ctx.scratch["candidates"] = [
        {
            "candidate_id": "base_big_but_leg_small",
            "adjusted_share": 100.0,
            "legs": [{"role": "home", "venue": "POLYMARKET", "price": 0.5, "share_if_wins": 40.0}],
        },
        {
            "candidate_id": "leg_big",
            "adjusted_share": 50.0,
            "legs": [
                {"role": "home", "venue": "POLYMARKET", "price": 0.5, "share_if_wins": 45.0},
                {"role": "away", "venue": "ORBITEXCH", "price": 2.0, "share_if_wins": 80.0},
            ],
        },
    ]

    _run(CandiSelectAction().execute(ctx))

    assert ctx.scratch["selected_candidate"]["candidate_id"] == "leg_big"
    assert [leg["share_if_wins"] for leg in ctx.scratch["legs"]] == [45.0, 80.0]


def test_no_candidates_clears_legs():
    ctx = EvalContext(pair_id="p")
    ctx.scratch["candidates"] = []
    ctx.scratch["legs"] = [{"role": "old"}]

    _run(CandiSelectAction().execute(ctx))

    assert ctx.scratch["legs"] == []


# ── #277:最小下注门控 ───────────────────────────────────────────────

def test_min_bet_gate_drops_pm_buy_below_min_quantity_before_scoring():
    """PM BUY 低于通用 min_quantity 时先淘汰，不参与选择。"""
    cache = _Cache({"H.POLYMARKET": _pm_instrument(min_quantity=5.0)})
    ctx = EvalContext(pair_id="p", cache=cache)
    ctx.scratch["candidates"] = [
        {"candidate_id": "tiny_qty_big_share", "legs": [_pm_leg(qty=3.0, share=100.0)]},
        {"candidate_id": "ok", "legs": [_pm_leg(qty=40.0, share=40.0)]},
    ]

    _run(CandiSelectAction().execute(ctx))

    assert ctx.scratch["selected_candidate"]["candidate_id"] == "ok"


def test_min_bet_gate_applies_pm_min_quantity_to_sell():
    cache = _Cache({"H.POLYMARKET": _pm_instrument(min_quantity=5.0)})
    ctx = EvalContext(pair_id="p", cache=cache)
    sell = _pm_leg(qty=0.5, share=0.5)
    sell["side"] = "SELL"
    ctx.scratch["candidates"] = [{"candidate_id": "reduce", "legs": [sell]}]

    _run(CandiSelectAction().execute(ctx))

    assert ctx.scratch["legs"] == []
    assert "selected_candidate" not in ctx.scratch


def test_min_bet_gate_drops_pm_buy_below_min_buy_notional():
    cache = _Cache({"H.POLYMARKET": _pm_instrument(min_buy_notional=1.0)})
    ctx = EvalContext(pair_id="p", cache=cache)
    # qty 6 >= min_quantity 5,但 6 * 0.1 = 0.6 USD < 1 USD BUY 金额门
    ctx.scratch["candidates"] = [
        {"candidate_id": "below_notional", "legs": [_pm_leg(qty=6.0, share=6.0, price=0.1)]},
    ]

    _run(CandiSelectAction().execute(ctx))

    assert ctx.scratch["legs"] == []
    assert "selected_candidate" not in ctx.scratch


def test_min_bet_gate_uses_stake_notional_for_decimal_venue():
    """OE 的 min_notional 是 stake 口径(NT notional_value = qty×multiplier,非 qty×price)。"""
    instrument = _oe_instrument(min_stake_usd=12.0)
    cache = _Cache({instrument.id: instrument})
    oe_leg = {
        "instrument_id": str(instrument.id),
        "venue": "ORBITEXCH",
        "price": 2.0,
        "qty": 10.0,        # stake 10 USD < 12;qty×price=20 会误判通过
        "share_if_wins": 20.0,
    }
    ctx = EvalContext(pair_id="p", cache=cache)
    ctx.scratch["candidates"] = [{"candidate_id": "stake_below", "legs": [dict(oe_leg)]}]

    _run(CandiSelectAction().execute(ctx))
    assert ctx.scratch["legs"] == []

    ctx2 = EvalContext(pair_id="p", cache=cache)
    ctx2.scratch["candidates"] = [
        {"candidate_id": "stake_ok", "legs": [dict(oe_leg, qty=13.0)]},
    ]
    _run(CandiSelectAction().execute(ctx2))
    assert ctx2.scratch["selected_candidate"]["candidate_id"] == "stake_ok"


def test_min_bet_gate_drops_whole_candidate_when_any_leg_fails():
    """双腿 candidate 只要一腿低于限额,整个 candidate 淘汰(半个套利不是套利)。"""
    cache = _Cache({"H.POLYMARKET": _pm_instrument(min_quantity=5.0)})
    ctx = EvalContext(pair_id="p", cache=cache)
    ctx.scratch["candidates"] = [
        {
            "candidate_id": "one_leg_tiny",
            "legs": [_pm_leg(qty=40.0, share=40.0), _pm_leg(qty=3.0, share=3.0)],
        },
    ]

    _run(CandiSelectAction().execute(ctx))

    assert ctx.scratch["legs"] == []


# ── #277:legs-only 包装(mean_rebate / comp 链)───────────────────

def test_legs_only_scratch_wrapped_as_single_candidate():
    legs = [_pm_leg(qty=8.0, share=8.0)]
    ctx = EvalContext(pair_id="p")     # cache None → 跳过 instrument 门,结构路径仍走全程
    ctx.scratch["legs"] = list(legs)

    _run(CandiSelectAction().execute(ctx))

    assert ctx.scratch["selected_candidate"]["candidate_id"] == "legs"
    assert ctx.scratch["legs"] == legs


def test_legs_only_below_min_cleared():
    cache = _Cache({"H.POLYMARKET": _pm_instrument(min_quantity=5.0)})
    ctx = EvalContext(pair_id="p", cache=cache)
    ctx.scratch["legs"] = [_pm_leg(qty=3.0, share=3.0)]

    _run(CandiSelectAction().execute(ctx))

    assert ctx.scratch["legs"] == []


def test_noop_without_candidates_and_legs():
    ctx = EvalContext(pair_id="p")

    _run(CandiSelectAction().execute(ctx))

    assert "legs" not in ctx.scratch
    assert "selected_candidate" not in ctx.scratch
