"""套利 opportunity metadata 编解码契约。"""

from types import SimpleNamespace

from src.arbitrage.common.opportunity import CancelOpportunityMeta
from src.arbitrage.common.opportunity import OpportunityMeta
from src.arbitrage.common.opportunity import cancel_meta_from_command
from src.arbitrage.common.opportunity import cancel_params_from_meta
from src.arbitrage.common.opportunity import meta_from_tags
from src.arbitrage.common.opportunity import order_score_tags
from src.arbitrage.common.opportunity import tags_from_meta


def test_opportunity_meta_round_trips_venue_required_balance():
    meta = OpportunityMeta(
        opportunity_id="opp-1",
        pair_id="pair-1",
        leg_key="pm:no:0:buy",
        expected_legs=("pm:no:0:reduce", "pm:no:0:buy"),
        positions_digest="positions-1",
        venue_required_balance=12.5,
    )

    assert meta_from_tags(tags_from_meta(meta)) == meta


def test_opportunity_meta_without_venue_total_remains_compatible():
    meta = OpportunityMeta(
        opportunity_id="opp-1",
        pair_id="pair-1",
        leg_key="pm:home:0",
        expected_legs=("pm:home:0",),
    )

    assert meta_from_tags(tags_from_meta(meta)) == meta


def test_opportunity_meta_round_trips_enable_timeout():
    meta = OpportunityMeta(
        opportunity_id="opp-1",
        pair_id="pair-1",
        leg_key="pm:home:0",
        expected_legs=("pm:home:0",),
        enable_timeout=False,
    )

    assert meta_from_tags(tags_from_meta(meta)) == meta


def test_opportunity_meta_round_trips_market():
    meta = OpportunityMeta(
        opportunity_id="opp-1",
        pair_id="pair-1",
        leg_key="pm:home:0",
        expected_legs=("pm:home:0",),
        market=True,
    )

    assert meta_from_tags(tags_from_meta(meta)) == meta


def test_order_score_tags_encode_score_and_sports_event_time():
    assert order_score_tags(" 7-6(7-4), 2-2 ", 1791820079000000000) == [
        "arb:order_score=7-6(7-4), 2-2",
        "arb:order_score_ts_ns=1791820079000000000",
    ]


def test_order_score_tags_keep_missing_values_empty():
    assert order_score_tags(None, None) == [
        "arb:order_score=",
        "arb:order_score_ts_ns=",
    ]


def test_order_score_tags_do_not_change_opportunity_meta_parsing():
    meta = OpportunityMeta(
        opportunity_id="opp-1",
        pair_id="pair-1",
        leg_key="pm:home:0",
        expected_legs=("pm:home:0",),
    )
    tags = tags_from_meta(meta) + order_score_tags("2-2", 123)

    assert meta_from_tags(tags) == meta


def test_cancel_opportunity_meta_round_trips_through_command_params():
    meta = CancelOpportunityMeta(
        opportunity_id="cancel-opp-1",
        pair_id="pair-1",
        cancel_key="order-a",
        expected_cancels=("order-a", "order-b"),
    )

    assert cancel_meta_from_command(
        SimpleNamespace(params=cancel_params_from_meta(meta)),
    ) == meta


def test_cancel_opportunity_meta_rejects_duplicate_expected_keys():
    meta = CancelOpportunityMeta(
        opportunity_id="cancel-opp-1",
        pair_id="pair-1",
        cancel_key="order-a",
        expected_cancels=("order-a", "order-a"),
    )

    assert cancel_meta_from_command(
        SimpleNamespace(params=cancel_params_from_meta(meta)),
    ) is None
