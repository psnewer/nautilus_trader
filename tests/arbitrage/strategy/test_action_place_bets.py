"""Slice 9(#49):`PlaceBetsAction` log-only smoke + size 算法(PM=share / OE=share/odds)。"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from nautilus_trader.adapters.orbitexch.data import oe_runner_to_book_deltas
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.identifiers import InstrumentId
from src.arbitrage import bootstrap
from src.arbitrage.strategy.actions.place_bets import PlaceBetsAction
from src.arbitrage.strategy.leg_plan import compute_size as _compute_size
from src.arbitrage.strategy.condition import EvalContext
from tests.arbitrage.strategy._live_state import live_context


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_pm_size_equals_share():
    assert _compute_size("POLYMARKET", 22.5, 0.40) == 22.5


def test_oe_size_is_share_over_odds():
    # share=22.5, odds=2.5 → stake = 9.0
    assert _compute_size("ORBITEXCH", 22.5, 2.5) == 9.0


def test_sharpexch_size_is_share_over_odds():
    assert _compute_size("SHARPEXCH", 22.5, 2.5) == 9.0


def test_oe_size_zero_when_price_invalid():
    assert _compute_size("ORBITEXCH", 22.5, 0) == 0.0
    assert _compute_size("ORBITEXCH", 22.5, -1) == 0.0


def test_action_log_only_no_raise_when_no_legs(caplog):
    """Check 没写 scratch["legs"] → Action 静默 skip,不 raise。"""
    ctx = EvalContext(pair_id="p")
    action = PlaceBetsAction()
    _run(action.execute(ctx))
    # 没下单 log(只 debug 级别 skip)


def test_action_logs_each_leg(caplog):
    ctx = EvalContext(pair_id="p")    # submitter=None → log-only fallback
    ctx.scratch["legs"] = [
        {"instrument_id": "H.POLYMARKET", "venue": "POLYMARKET", "side": "BUY",
         "role": "home", "price": 0.4, "prob": 0.4, "share_if_wins": 22.5},
        {"instrument_id": "A.ORBITEXCH", "venue": "ORBITEXCH", "side": "BUY",
         "role": "away", "price": 2.5, "prob": 0.4, "share_if_wins": 22.5},
    ]
    ctx.scratch["mean_rebate_rate"] = 0.15

    with caplog.at_level(logging.INFO, logger="src.arbitrage.strategy.actions.place_bets"):
        _run(PlaceBetsAction().execute(ctx))

    msgs = [r.message for r in caplog.records]
    assert any("PlaceBets[smoke]" in m and "legs=2" in m for m in msgs)
    assert any("H.POLYMARKET" in m and "qty=22.5000" in m for m in msgs)
    assert any("A.ORBITEXCH" in m and "qty=9.0000" in m for m in msgs)


# ── slice 10a(#50):submitter 接通 ─────────────────────────────────

def test_action_calls_submitter_when_present(caplog):
    """ctx.submitter 非 None → Action 经 `await submitter(spec)` 真出单(不走 log-only fallback)。"""
    calls = []
    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["legs"] = [
        {"instrument_id": "H.POLYMARKET", "venue": "POLYMARKET", "side": "BUY",
         "role": "home", "price": 0.4, "prob": 0.4, "share_if_wins": 22.5},
        {"instrument_id": "A.ORBITEXCH", "venue": "ORBITEXCH", "side": "BUY",
         "role": "away", "price": 2.5, "prob": 0.4, "share_if_wins": 22.5},
    ]
    ctx.scratch["mean_rebate_rate"] = 0.15

    with caplog.at_level(logging.INFO, logger="src.arbitrage.strategy.actions.place_bets"):
        _run(PlaceBetsAction().execute(ctx))

    # 2 leg → 2 submitter 调用
    assert len(calls) == 2
    # PM leg:size=22.5,price=0.4
    assert calls[0]["instrument_id"] == "H.POLYMARKET"
    assert calls[0]["side"] == "BUY"
    assert calls[0]["qty"] == 22.5
    assert calls[0]["price"] == 0.4
    assert calls[0]["intent"] == "arbitrage"
    assert calls[0]["opportunity_id"] == calls[1]["opportunity_id"]
    assert calls[0]["pair_id"] == "p"
    assert calls[0]["leg_key"] != calls[1]["leg_key"]
    assert tuple(calls[0]["expected_legs"]) == tuple(calls[1]["expected_legs"])
    assert calls[0]["leg_key"] in calls[0]["expected_legs"]
    # OE leg:size=22.5/2.5=9.0,price=2.5
    assert calls[1]["instrument_id"] == "A.ORBITEXCH"
    assert calls[1]["qty"] == 9.0
    assert calls[1]["price"] == 2.5
    # log mode = "submit" 不是 "smoke"
    msgs = [r.message for r in caplog.records]
    assert any("PlaceBets[submit]" in m for m in msgs)
    # log-only fallback "would submit" 不应出现
    assert not any("would submit" in m for m in msgs)


def test_action_uses_leg_qty_when_check_precomputes_size():
    """recovery Check 可把补缺口 qty 写进 leg,复用 place_bets 出补救单。"""
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["legs"] = [
        {"instrument_id": "A.ORBITEXCH", "venue": "ORBITEXCH", "side": "BUY",
         "role": "away", "price": 2.5, "prob": 0.4, "qty": 3.25},
    ]

    _run(PlaceBetsAction().execute(ctx))

    assert calls == [{
        "instrument_id": "A.ORBITEXCH",
        "side": "BUY",
        "qty": 3.25,
        "price": 2.5,
        "intent": "arbitrage",
        "opportunity_id": calls[0]["opportunity_id"],
        "pair_id": "p",
        "leg_key": "orbitexch:away:0",
        "expected_legs": ("orbitexch:away:0",),
        "open_orders_digest": None,
        "positions_digest": None,
        "venue_required_balance": 3.25,
    }]


def test_action_uses_leg_share_if_wins_without_action_share():
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["legs"] = [
        {"instrument_id": "H.POLYMARKET", "venue": "POLYMARKET", "side": "BUY",
         "role": "home", "price": 0.4, "prob": 0.4, "share_if_wins": 40.0},
        {"instrument_id": "A.ORBITEXCH", "venue": "ORBITEXCH", "side": "BUY",
         "role": "away", "price": 2.5, "prob": 0.4, "share_if_wins": 40.0},
    ]

    _run(PlaceBetsAction().execute(ctx))

    assert calls[0]["qty"] == 40.0
    assert calls[1]["qty"] == 16.0


def test_action_uses_sharpexch_leg_share_if_wins_without_action_share():
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["legs"] = [
        {"instrument_id": "A.SHARPEXCH", "venue": "SHARPEXCH", "side": "BUY",
         "role": "away", "price": 2.5, "prob": 0.4, "share_if_wins": 40.0},
    ]

    _run(PlaceBetsAction().execute(ctx))

    assert calls[0]["qty"] == 16.0
    assert calls[0]["leg_key"] == "sharpexch:away:0"


def test_decimal_real_no_claim_keeps_back_side_price_and_size():
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["legs"] = [
        {
            "instrument_id": "A.SHARPEXCH",
            "venue": "SHARPEXCH",
            "side": "BUY",
            "role": "away",
            "claim": "no",
            "price": 2.0,
            "bid": 2.1,
            "qty": 999.0,
            "share_if_wins": 21.0,
        },
    ]

    _run(PlaceBetsAction().execute(ctx))

    assert calls[0]["side"] == "BUY"
    assert calls[0]["price"] == 2.0
    assert calls[0]["qty"] == 999.0


def test_decimal_no_claim_redirects_to_exec_instrument():
    """#228:合成 no 腿(-NO instrument)带 exec_instrument_id → 真单落在同 selection 的 yes instrument。"""
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["legs"] = [
        {
            "instrument_id": "1-123--43-None.ORBITEXCH",       # 合成 no instrument(行情/身份载体)
            "exec_instrument_id": "1-123-42-None.ORBITEXCH",  # 同 selection 的 yes instrument
            "venue": "ORBITEXCH",
            "side": "BUY",
            "role": "no",
            "claim": "no",
            "price": 2.5,        # no book ask = lay 原值
            "lay_price": 2.5,
            "share_if_wins": 25.0,
        },
    ]

    _run(PlaceBetsAction().execute(ctx))

    assert calls[0]["instrument_id"] == "1-123-42-None.ORBITEXCH"
    assert calls[0]["side"] == "SELL"
    assert calls[0]["price"] == 2.5
    assert calls[0]["qty"] == 10.0   # share_if_wins / lay = 25 / 2.5


def test_probability_no_claim_keeps_buy_path():
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["legs"] = [
        {
            "instrument_id": "H.POLYMARKET",
            "venue": "POLYMARKET",
            "side": "BUY",
            "role": "home",
            "claim": "no",
            "price": 0.4,
            "bid": 0.39,
            "share_if_wins": 21.0,
        },
    ]

    _run(PlaceBetsAction().execute(ctx))

    assert calls[0]["side"] == "BUY"
    assert calls[0]["price"] == 0.4
    assert calls[0]["qty"] == 21.0


def test_action_can_override_venue_price_and_qty_for_live_probe():
    """临时 live 验证可覆盖 OE 下单价/量,但不影响 Check 使用真实 order book 算机会。"""
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["legs"] = [
        {"instrument_id": "A.ORBITEXCH", "venue": "ORBITEXCH", "side": "BUY",
         "role": "away", "price": 2.5, "prob": 0.4},
    ]

    action = PlaceBetsAction(
        price_overrides={"orbitexch": 1000.0},
        qty_overrides={"ORBITEXCH": 7.0},
    )
    _run(action.execute(ctx))

    assert calls == [{
        "instrument_id": "A.ORBITEXCH",
        "side": "BUY",
        "qty": 7.0,
        "price": 1000.0,
        "intent": "arbitrage",
        "opportunity_id": calls[0]["opportunity_id"],
        "pair_id": "p",
        "leg_key": "orbitexch:away:0",
        "expected_legs": ("orbitexch:away:0",),
        "open_orders_digest": None,
        "positions_digest": None,
        "venue_required_balance": 7.0,
    }]


class _Qty:
    def __init__(self, value):
        self._value = value

    def as_double(self):
        return self._value


def _pm_inventory_ctx(
    *,
    held_qty: float,
    min_quantity: float = 5.0,
    min_buy_notional: float = 1.0,
):
    target = "ALCARAZ.POLYMARKET"
    opposite = "SINNER.POLYMARKET"
    return live_context(
        instrument_ids=[target, opposite],
        positions=[SimpleNamespace(instrument_id=opposite, side="LONG", quantity=_Qty(held_qty))],
        infos={
            target: {"selection_role": "away"},
            opposite: {"selection_role": "home"},
        },
        constraints={
            target: {
                "min_quantity": min_quantity,
                "min_notional": None,
                "min_buy_notional": min_buy_notional,
                "size_increment": 0.01,
            },
            opposite: {
                "min_quantity": min_quantity,
                "min_notional": None,
                "min_buy_notional": min_buy_notional,
                "size_increment": 0.01,
            },
        },
    )


def _pm_target_ctx(ctx, *, price: float = 0.2):
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx.submitter = fake_submitter
    ctx.scratch["legs"] = [{
        "instrument_id": "ALCARAZ.POLYMARKET",
        "venue": "POLYMARKET",
        "side": "BUY",
        "role": "away",
        "price": price,
        "share_if_wins": 100.0,
    }]
    return ctx, calls


def test_probability_buy_splits_into_opposite_sell_and_remainder_buy():
    ctx, calls = _pm_target_ctx(_pm_inventory_ctx(held_qty=60))

    _run(PlaceBetsAction().execute(ctx))

    assert [(c["instrument_id"], c["side"], c["qty"], c["price"]) for c in calls] == [
        ("SINNER.POLYMARKET", "SELL", 60.0, 0.8),
        ("ALCARAZ.POLYMARKET", "BUY", 40.0, 0.2),
    ]
    assert calls[0]["expected_legs"] == calls[1]["expected_legs"]
    assert set(calls[0]["expected_legs"]) == {
        "polymarket:away:0:reduce",
        "polymarket:away:0:buy",
    }
    assert calls[0]["venue_required_balance"] == 8.0
    assert calls[1]["venue_required_balance"] == 8.0


def test_probability_split_adjusts_reduction_to_keep_minimum_buy_quantity():
    ctx, calls = _pm_target_ctx(_pm_inventory_ctx(held_qty=97))

    _run(PlaceBetsAction().execute(ctx))

    assert [(c["side"], c["qty"]) for c in calls] == [("SELL", 95.0), ("BUY", 5.0)]


def test_probability_split_keeps_minimum_buy_notional_at_low_price():
    ctx, calls = _pm_target_ctx(_pm_inventory_ctx(held_qty=97), price=0.02)

    _run(PlaceBetsAction().execute(ctx))

    assert [(c["side"], c["qty"], c["price"]) for c in calls] == [
        ("SELL", 50.0, 0.98),
        ("BUY", 50.0, 0.02),
    ]


def test_probability_split_falls_back_to_direct_buy_when_reduction_is_below_minimum():
    ctx, calls = _pm_target_ctx(_pm_inventory_ctx(held_qty=3))

    _run(PlaceBetsAction().execute(ctx))

    assert [(c["instrument_id"], c["side"], c["qty"]) for c in calls] == [
        ("ALCARAZ.POLYMARKET", "BUY", 100.0),
    ]
    assert calls[0]["venue_required_balance"] == 20.0


def test_probability_buy_fully_replaced_by_opposite_sell():
    ctx, calls = _pm_target_ctx(_pm_inventory_ctx(held_qty=100))

    _run(PlaceBetsAction().execute(ctx))

    assert [(c["instrument_id"], c["side"], c["qty"], c["price"]) for c in calls] == [
        ("SINNER.POLYMARKET", "SELL", 100.0, 0.8),
    ]
    assert calls[0]["venue_required_balance"] == 0.0


def test_action_qty_override_beats_leg_qty():
    """显式 venue qty override 是 live probe 开关,优先级高于 Check 写出的 qty。"""
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["legs"] = [
        {"instrument_id": "A.ORBITEXCH", "venue": "ORBITEXCH", "side": "BUY",
         "role": "away", "price": 2.5, "prob": 0.4, "qty": 3.25},
    ]

    _run(PlaceBetsAction(qty_overrides={"ORBITEXCH": 7.0}).execute(ctx))

    assert calls[0]["qty"] == 7.0


def test_action_can_mark_recovery_intent():
    """compensation tree 可把补救单标成 recovery,供 Risk 跳过 rebate gates。"""
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["legs"] = [
        {"instrument_id": "H.POLYMARKET", "venue": "POLYMARKET", "side": "BUY",
         "role": "home", "price": 0.4, "prob": 0.4, "share_if_wins": 5.0},
    ]

    _run(PlaceBetsAction(intent="recovery").execute(ctx))

    assert calls[0]["intent"] == "recovery"
    assert calls[0]["qty"] == 5.0


def test_submitter_none_falls_back_to_log_only():
    """显式确认:submitter=None 走 log-only,无 raise(已被 test_action_logs_each_leg 覆盖,但显式再确认)。"""
    ctx = EvalContext(pair_id="p", submitter=None)
    ctx.scratch["legs"] = [{"instrument_id": "X", "venue": "POLYMARKET", "side": "BUY",
                             "role": "home", "price": 0.5, "prob": 0.5, "share_if_wins": 10.0}]
    _run(PlaceBetsAction().execute(ctx))   # 不 raise


def test_action_aborts_when_leg_missing_qty_and_share_if_wins(caplog):
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["legs"] = [
        {"instrument_id": "H.POLYMARKET", "venue": "POLYMARKET", "side": "BUY",
         "role": "home", "price": 0.4, "prob": 0.4},
    ]

    with caplog.at_level(logging.WARNING, logger="src.arbitrage.strategy.actions.place_bets"):
        _run(PlaceBetsAction().execute(ctx))

    assert calls == []
    assert any("missing qty/share_if_wins" in r.message for r in caplog.records)


def test_action_aborts_when_leg_is_non_tradable_anchor(caplog):
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["legs"] = [
        {"instrument_id": "5843495.PMSPORTS", "venue": "PMSPORTS", "side": "BUY",
         "role": "event", "price": 1.0, "qty": 1.0, "tradable": False, "anchor": True},
    ]

    with caplog.at_level(logging.WARNING, logger="src.arbitrage.strategy.actions.place_bets"):
        _run(PlaceBetsAction().execute(ctx))

    assert calls == []
    assert any("non-tradable anchor" in r.message for r in caplog.records)


# ── #256 续:market_order_enabled 最差价覆盖 ─────────────────────────

def _oe_book_with_depth(iid_str: str, *, back, lay, claim: str = "yes") -> OrderBook:
    """经真实 `oe_runner_to_book_deltas` 构造多档 book(概率编码,claim 决定 ask/bid 换位)。"""
    iid = InstrumentId.from_str(iid_str)
    runner = {
        "back": [{"price": p, "size": 10.0} for p in back],
        "lay": [{"price": p, "size": 10.0} for p in lay],
    }
    deltas = oe_runner_to_book_deltas(iid, runner, ts_init_ns=1000, claim=claim)
    book = OrderBook(iid, BookType.L2_MBP)
    book.apply_deltas(deltas)
    return book


def test_market_order_disabled_uses_original_price():
    """默认关闭:即使 book 有更深档位,提交价仍是 Check 算出的原始限价(最优价)。"""
    bootstrap.reset_arb_context()
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    iid = "A.ORBITEXCH"
    book = _oe_book_with_depth(iid, back=[2.5, 2.0], lay=[2.6, 2.7])
    ctx = live_context(
        instrument_ids=[iid], books={iid: book},
        infos={iid: {"selection_role": "away"}}, submitter=fake_submitter,
    )
    ctx.scratch["legs"] = [
        {"instrument_id": iid, "venue": "ORBITEXCH", "side": "BUY",
         "role": "away", "price": 2.5, "prob": 0.4, "share_if_wins": 10.0},
    ]

    _run(PlaceBetsAction().execute(ctx))

    assert calls[0]["price"] == 2.5
    bootstrap.reset_arb_context()


def test_market_order_enabled_uses_worst_back_price():
    """提交价用 book 内最差 back 价(最低赔率),但 qty 仍按候选最优价算。"""
    bootstrap.reset_arb_context()
    bootstrap.prepare_arb_context(market_order_enabled=True)
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    iid = "A.ORBITEXCH"
    book = _oe_book_with_depth(iid, back=[2.5, 2.0], lay=[2.6, 2.7])
    ctx = live_context(
        instrument_ids=[iid], books={iid: book},
        infos={iid: {"selection_role": "away"}}, submitter=fake_submitter,
    )
    ctx.scratch["legs"] = [
        {"instrument_id": iid, "venue": "ORBITEXCH", "side": "BUY",
         "role": "away", "price": 2.5, "prob": 0.4, "share_if_wins": 10.0},
    ]

    _run(PlaceBetsAction().execute(ctx))

    assert calls[0]["price"] == 2.0  # 提交价 = worst back(最低赔率)
    assert calls[0]["qty"] == 4.0    # qty 按候选最优价 2.5 算:10/2.5(不是按最差价 2.0 算的 5.0)
    bootstrap.reset_arb_context()


def test_market_order_enabled_uses_worst_lay_price_for_synthetic_no_leg():
    """合成 no 腿(exec_instrument_id 重定向):提交价用定价 instrument book 的 worst lay
    (ask←lay 列),提交目标仍是 exec_instrument_id,side 仍 SELL;qty 仍按候选最优价算。"""
    bootstrap.reset_arb_context()
    bootstrap.prepare_arb_context(market_order_enabled=True)
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    quote_iid = "ANO.ORBITEXCH"
    exec_iid = "A.ORBITEXCH"
    book = _oe_book_with_depth(quote_iid, back=[2.6, 2.7], lay=[2.5, 2.0], claim="no")
    ctx = live_context(
        instrument_ids=[quote_iid], books={quote_iid: book},
        infos={quote_iid: {
            "selection_role": "away", "claim": "no", "quote_claim": "no",
            "exec_instrument_id": exec_iid,
        }},
        submitter=fake_submitter,
    )
    ctx.scratch["legs"] = [
        {"instrument_id": quote_iid, "venue": "ORBITEXCH", "side": "BUY",
         "role": "away", "price": 2.0, "prob": 0.5, "share_if_wins": 10.0,
         "claim": "no", "lay_price": 2.0, "exec_instrument_id": exec_iid},
    ]

    _run(PlaceBetsAction().execute(ctx))

    assert calls[0]["instrument_id"] == exec_iid
    assert calls[0]["side"] == "SELL"
    assert calls[0]["price"] == 2.5  # 提交价 = worst lay(最高赔率)
    assert calls[0]["qty"] == 5.0    # qty 按候选最优价 2.0 算:10/2.0(不是按最差价 2.5 算的 4.0)
    bootstrap.reset_arb_context()


def test_market_order_missing_depth_falls_back_to_original_price():
    """打开但该 instrument 还没收到过赔率帧(live cache 无 book)→ 退回原限价,不报错。"""
    bootstrap.reset_arb_context()
    bootstrap.prepare_arb_context(market_order_enabled=True)
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["legs"] = [
        {"instrument_id": "A.ORBITEXCH", "venue": "ORBITEXCH", "side": "BUY",
         "role": "away", "price": 2.5, "prob": 0.4, "share_if_wins": 10.0},
    ]

    _run(PlaceBetsAction().execute(ctx))

    assert calls[0]["price"] == 2.5
    bootstrap.reset_arb_context()


def test_market_order_explicit_price_override_takes_precedence():
    """显式 price_overrides(调试/live probe)优先于市价单开关,不被替换。"""
    bootstrap.reset_arb_context()
    bootstrap.prepare_arb_context(market_order_enabled=True)
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    iid = "A.ORBITEXCH"
    book = _oe_book_with_depth(iid, back=[2.5, 2.0], lay=[2.6, 2.7])
    ctx = live_context(
        instrument_ids=[iid], books={iid: book},
        infos={iid: {"selection_role": "away"}}, submitter=fake_submitter,
    )
    ctx.scratch["legs"] = [
        {"instrument_id": iid, "venue": "ORBITEXCH", "side": "BUY",
         "role": "away", "price": 2.5, "prob": 0.4, "share_if_wins": 10.0},
    ]

    _run(PlaceBetsAction(price_overrides={"orbitexch": 1000.0}).execute(ctx))

    assert calls[0]["price"] == 1000.0
    bootstrap.reset_arb_context()


def test_market_order_does_not_apply_to_probability_venue():
    """开关只对 decimal venue 生效;PM 腿价格不受影响。"""
    bootstrap.reset_arb_context()
    bootstrap.prepare_arb_context(market_order_enabled=True)
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["legs"] = [
        {"instrument_id": "H.POLYMARKET", "venue": "POLYMARKET", "side": "BUY",
         "role": "home", "price": 0.4, "prob": 0.4, "share_if_wins": 10.0},
    ]

    _run(PlaceBetsAction().execute(ctx))

    assert calls[0]["price"] == 0.4
    bootstrap.reset_arb_context()


# ── #277:intent 优先读 selected_candidate ────────────────────────

def test_intent_from_selected_candidate_overrides_configured():
    """recovery 候选经 arb 链胜出时,提交 intent 必须是 candidate 自带的 recovery。"""
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["selected_candidate"] = {"candidate_id": "recovery", "intent": "recovery"}
    ctx.scratch["legs"] = [
        {"instrument_id": "A.ORBITEXCH", "venue": "ORBITEXCH", "side": "BUY",
         "role": "away", "price": 2.5, "prob": 0.4, "qty": 3.25},
    ]

    _run(PlaceBetsAction(intent="arbitrage").execute(ctx))

    assert [spec["intent"] for spec in calls] == ["recovery"]


def test_intent_falls_back_to_configured_without_candidate_tag():
    """selected_candidate 无 intent 标记(常规 arb 候选)→ 用 Action 配置值。"""
    calls = []

    async def fake_submitter(spec: dict) -> None:
        calls.append(spec)

    ctx = EvalContext(pair_id="p", submitter=fake_submitter)
    ctx.scratch["selected_candidate"] = {"candidate_id": "arb"}
    ctx.scratch["legs"] = [
        {"instrument_id": "A.ORBITEXCH", "venue": "ORBITEXCH", "side": "BUY",
         "role": "away", "price": 2.5, "prob": 0.4, "qty": 3.25},
    ]

    _run(PlaceBetsAction(intent="arbitrage").execute(ctx))

    assert [spec["intent"] for spec in calls] == ["arbitrage"]
