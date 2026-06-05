"""Slice 9(utils):PM/OE prob 转换函数(`polymarket_price_to_probability` / `orbitexch_odds_to_probability`)。

API 详见 `src/arbitrage/common/utils_api.md`。
"""

from src.arbitrage.common.utils import orbitexch_odds_to_probability
from src.arbitrage.common.utils import polymarket_price_to_probability


# ── PM ──────────────────────────────────────────────────────────

def test_polymarket_price_passthrough():
    assert polymarket_price_to_probability(0.4) == 0.4
    assert polymarket_price_to_probability(0.5) == 0.5
    assert polymarket_price_to_probability(0.999) == 0.999


def test_polymarket_price_zero_or_negative_returns_zero():
    assert polymarket_price_to_probability(0) == 0.0
    assert polymarket_price_to_probability(-0.1) == 0.0


def test_polymarket_price_clamp_above_one():
    assert polymarket_price_to_probability(1.0) == 1.0
    assert polymarket_price_to_probability(1.5) == 1.0


def test_polymarket_price_dirty_data():
    assert polymarket_price_to_probability(None) == 0.0
    assert polymarket_price_to_probability("not_a_number") == 0.0


# ── OE ──────────────────────────────────────────────────────────

def test_orbitexch_odds_inverse():
    assert orbitexch_odds_to_probability(2.0) == 0.5
    assert abs(orbitexch_odds_to_probability(2.26) - 1.0 / 2.26) < 1e-9
    assert abs(orbitexch_odds_to_probability(4.0) - 0.25) < 1e-9


def test_orbitexch_odds_invalid_returns_zero():
    assert orbitexch_odds_to_probability(1.0) == 0.0      # stake <= 1 不可能
    assert orbitexch_odds_to_probability(0.8) == 0.0
    assert orbitexch_odds_to_probability(0) == 0.0
    assert orbitexch_odds_to_probability(-1) == 0.0


def test_orbitexch_odds_dirty_data():
    assert orbitexch_odds_to_probability(None) == 0.0
    assert orbitexch_odds_to_probability("not_a_number") == 0.0
