"""套利 opportunity metadata 编解码契约。"""

from src.arbitrage.common.opportunity import OpportunityMeta
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
