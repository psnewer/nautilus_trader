"""套利 opportunity metadata 编解码契约。"""

from types import SimpleNamespace

from src.arbitrage.common.opportunity import CancelOpportunityMeta
from src.arbitrage.common.opportunity import OpportunityMeta
from src.arbitrage.common.opportunity import cancel_meta_from_command
from src.arbitrage.common.opportunity import cancel_params_from_meta
from src.arbitrage.common.opportunity import meta_from_tags
from src.arbitrage.common.opportunity import tags_from_meta


def test_opportunity_meta_round_trips_venue_required_balance():
    meta = OpportunityMeta(
        opportunity_id="opp-1",
        pair_id="pair-1",
        leg_key="pm:no:0:buy",
        expected_legs=("pm:no:0:reduce", "pm:no:0:buy"),
        open_orders_digest="digest-1",
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
        enable_timeout=True,
    )

    assert meta_from_tags(tags_from_meta(meta)) == meta


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
