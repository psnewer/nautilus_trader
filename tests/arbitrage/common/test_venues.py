import msgspec
import pytest

from src.arbitrage.common.venues import DATA_SOURCE_REGISTRY
from src.arbitrage.common.venues import ORBITEXCH
from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.common.venues import SHARPEXCH
from src.arbitrage.common.venues import SPORTS_CLIENT
from src.arbitrage.common.venues import PositionOutcomeInvariantError
from src.arbitrage.common.venues import descriptor_for
from src.arbitrage.common.venues import enabled_data_source_client_ids
from src.arbitrage.common.venues import enabled_settlement_venues
from src.arbitrage.common.venues import enabled_sports_client_ids
from src.arbitrage.common.venues import enabled_tradable_venue_ids
from src.arbitrage.common.venues import enabled_venue_ids
from src.arbitrage.common.venues import is_decimal_odds_venue
from src.arbitrage.common.venues import is_known_venue
from src.arbitrage.common.venues import is_probability_odds_venue
from src.arbitrage.common.venues import is_venue_enabled
from src.arbitrage.common.venues import leg_economics
from src.arbitrage.common.venues import order_exposure_probability
from src.arbitrage.common.venues import order_required_balance
from src.arbitrage.common.venues import outcome_for_position
from src.arbitrage.common.venues import probability_from_price
from src.arbitrage.common.venues import qty_from_share
from src.arbitrage.common.venues import venue_id_from_instrument_id
from src.arbitrage.common.venues import venue_id_from_leg_key
from src.arbitrage.common.venues import venue_preference_rank
from src.arbitrage.config.schema import ArbConfig


class _FakeVenue:
    value = SHARPEXCH


class _FakeInstrumentId:
    venue = _FakeVenue()


def _cfg(**overrides) -> ArbConfig:
    return msgspec.convert(overrides, type=ArbConfig)


def test_static_descriptors_capture_current_venue_capabilities():
    pm = descriptor_for(POLYMARKET)
    oe = descriptor_for(ORBITEXCH)
    se = descriptor_for(SHARPEXCH)

    assert pm.odds_model == "probability"
    assert pm.settlement_kind == "polymarket_ctf"
    assert pm.discovery_config_builder is None
    assert oe.odds_model == "decimal"
    assert oe.discovery_config_builder == "to_oe_scraper_config"
    assert se.odds_model == "decimal"
    assert se.discovery_config_builder == "to_se_discovery_config"
    assert oe.venue_id != se.venue_id


def test_static_data_source_descriptors_capture_pmsports():
    sports = DATA_SOURCE_REGISTRY["sports_status"]

    assert sports.client_id == SPORTS_CLIENT
    assert sports.provider == "polymarket_sports"
    assert sports.data_config_builder == "to_sports_data_client_config"


def test_enabled_venues_follow_config():
    cfg = _cfg(venues={"orbitexch": {"enabled": False}, "sharpexch": {"enabled": True}})

    assert enabled_venue_ids(cfg) == (POLYMARKET, SHARPEXCH)
    assert enabled_tradable_venue_ids(cfg) == (POLYMARKET, SHARPEXCH)
    assert enabled_sports_client_ids(cfg) == ("PMSPORTS",)
    assert enabled_data_source_client_ids(cfg) == ("PMSPORTS",)
    assert is_venue_enabled(cfg, POLYMARKET) is True
    assert is_venue_enabled(cfg, ORBITEXCH) is False
    assert is_venue_enabled(cfg, SHARPEXCH) is True


def test_enabled_data_sources_are_independent_from_polymarket_venue():
    cfg = _cfg(venues={"polymarket": {"enabled": False}, "orbitexch": {"enabled": True}, "sharpexch": {"enabled": True}})

    assert enabled_venue_ids(cfg) == (ORBITEXCH, SHARPEXCH)
    assert enabled_tradable_venue_ids(cfg) == (ORBITEXCH, SHARPEXCH)
    assert enabled_sports_client_ids(cfg) == ("PMSPORTS",)


def test_enabled_data_sources_follow_data_source_config():
    cfg = _cfg(data_sources={"sports_status": {"enabled": False}})

    assert enabled_sports_client_ids(cfg) == ()


def test_enabled_data_sources_require_matching_provider():
    cfg = _cfg(data_sources={"sports_status": {"enabled": True, "provider": "other_provider"}})

    assert enabled_sports_client_ids(cfg) == ()


def test_enabled_settlement_venues_follow_capability_and_enablement():
    cfg = _cfg()

    assert tuple(v.venue_id for v in enabled_settlement_venues(cfg, "polymarket_ctf")) == (POLYMARKET,)


def test_enabled_settlement_venues_skip_disabled_capability_owner():
    cfg = _cfg(
        venues={
            "polymarket": {"enabled": False},
            "orbitexch": {"enabled": True},
            "sharpexch": {"enabled": True},
        },
    )

    assert enabled_settlement_venues(cfg, "polymarket_ctf") == ()


@pytest.mark.parametrize("venue", [ORBITEXCH, SHARPEXCH])
def test_decimal_helpers_are_shared_for_oe_and_se(venue):
    assert is_decimal_odds_venue(venue) is True
    assert probability_from_price(venue, 4.0) == 0.25
    assert qty_from_share(venue, 100.0, 4.0) == 25.0


@pytest.mark.parametrize("venue", [ORBITEXCH, SHARPEXCH])
def test_decimal_no_claim_probability_is_complement(venue):
    """#228:decimal venue claim=no 腿 book 存 lay 原值,隐含概率 = 1 − 1/price。"""
    assert probability_from_price(venue, 4.0, claim="no") == 0.75
    assert probability_from_price(venue, 4.0, claim="yes") == 0.25
    assert probability_from_price(venue, 4.0) == 0.25  # 默认 yes,向后兼容


@pytest.mark.parametrize(
    ("venue", "side", "price", "expected"),
    [
        (POLYMARKET, "BUY", 0.40, 0.40),
        (POLYMARKET, "SELL", 0.96, 0.04),
        (ORBITEXCH, "BUY", 2.00, 0.50),
        (ORBITEXCH, "SELL", 4.00, 0.75),
    ],
)
def test_order_exposure_probability(venue, side, price, expected):
    assert order_exposure_probability(venue, price, side) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("venue", "side", "quantity", "price", "expected"),
    [
        (POLYMARKET, "BUY", 10.0, 0.40, 4.0),
        (POLYMARKET, "SELL", 10.0, 0.40, 0.0),
        (ORBITEXCH, "BUY", 10.0, 3.00, 10.0),
        (ORBITEXCH, "SELL", 10.0, 3.00, 20.0),
    ],
)
def test_order_required_balance(venue, side, quantity, price, expected):
    assert order_required_balance(venue, quantity, price, side) == pytest.approx(expected)


def test_polymarket_helpers_use_probability_share_semantics():
    assert is_decimal_odds_venue(POLYMARKET) is False
    assert is_probability_odds_venue(POLYMARKET) is True
    assert probability_from_price(POLYMARKET, 0.25) == 0.25
    assert probability_from_price(POLYMARKET, 0.25, claim="no") == 0.25  # probability venue 不受 claim 影响
    assert qty_from_share(POLYMARKET, 100.0, 0.25) == 100.0


def test_position_outcome_maps_pm_no_long_to_no():
    assert outcome_for_position(
        POLYMARKET,
        ["yes", "no"],
        selection_role="home",
        claim="no",
        position_side="LONG",
    ) == "no"


@pytest.mark.parametrize("venue", [ORBITEXCH, SHARPEXCH])
def test_position_outcome_maps_decimal_short_to_binary_complement(venue):
    assert outcome_for_position(
        venue,
        ["yes", "no"],
        selection_role="home",
        claim="yes",
        position_side="SHORT",
    ) == "no"


def test_position_outcome_rejects_probability_short_and_nonbinary_decimal_short():
    with pytest.raises(PositionOutcomeInvariantError, match="cannot be SHORT"):
        outcome_for_position(
            POLYMARKET,
            ["yes", "no"],
            selection_role="home",
            claim="yes",
            position_side="SHORT",
        )
    assert outcome_for_position(
        ORBITEXCH,
        ["home", "draw", "away"],
        selection_role="home",
        claim=None,
        position_side="SHORT",
    ) is None


def test_leg_economics_covers_probability_back_decimal_back_and_lay():
    pm = leg_economics(POLYMARKET, price=0.4, size=10.0)
    assert pm.share_if_wins == pytest.approx(10.0)
    assert pm.profit_if_wins == pytest.approx(6.0)
    assert pm.loss_if_loses == pytest.approx(4.0)

    back = leg_economics(ORBITEXCH, price=2.5, size=4.0)
    assert back.share_if_wins == pytest.approx(10.0)
    assert back.profit_if_wins == pytest.approx(6.0)
    assert back.loss_if_loses == pytest.approx(4.0)

    lay = leg_economics(SHARPEXCH, price=2.5, size=4.0, is_lay=True)
    assert lay.share_if_wins == pytest.approx(10.0)
    assert lay.profit_if_wins == pytest.approx(4.0)
    assert lay.loss_if_loses == pytest.approx(6.0)


def test_is_known_venue_checks_static_registry():
    assert is_known_venue(POLYMARKET) is True
    assert is_known_venue("unknown") is False


def test_venue_preference_rank_prefers_probability_then_registry_order():
    ordered = sorted([SHARPEXCH, POLYMARKET, ORBITEXCH], key=venue_preference_rank)

    assert ordered == [POLYMARKET, ORBITEXCH, SHARPEXCH]


def test_venue_id_from_instrument_id_prefers_nt_venue_attribute():
    assert venue_id_from_instrument_id(_FakeInstrumentId()) == SHARPEXCH


def test_venue_id_from_instrument_id_accepts_legacy_string_suffix():
    assert venue_id_from_instrument_id("123.ORBITEXCH") == ORBITEXCH
    assert venue_id_from_instrument_id("123.UNKNOWN") == ""


@pytest.mark.parametrize(
    ("leg_key", "expected"),
    [
        ("pm:home:0", POLYMARKET),
        ("oe:away:1", ORBITEXCH),
        ("se:away:1", SHARPEXCH),
        ("polymarket:home:0", POLYMARKET),
        ("orbitexch:away:1", ORBITEXCH),
        ("sharpexch:away:1", SHARPEXCH),
        ("UNKNOWN:away:1", None),
    ],
)
def test_venue_id_from_leg_key_accepts_full_names_and_legacy_aliases(leg_key, expected):
    assert venue_id_from_leg_key(leg_key) == expected
