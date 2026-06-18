"""VenueExecutionLiveness 共享状态语义。"""

from src.arbitrage.common.venue_liveness import VenueExecutionLiveness


def test_unknown_venue_defaults_not_alive():
    liveness = VenueExecutionLiveness()
    assert not liveness.order_alive("POLYMARKET")
    assert not liveness.position_alive("POLYMARKET")
    assert not liveness.venue_alive("POLYMARKET")


def test_venue_alive_requires_order_and_position_alive():
    liveness = VenueExecutionLiveness(("POLYMARKET",))
    liveness.mark_order_alive("POLYMARKET")
    assert liveness.order_alive("POLYMARKET")
    assert not liveness.venue_alive("POLYMARKET")

    liveness.mark_position_alive("POLYMARKET")
    assert liveness.venue_alive("POLYMARKET")

    liveness.mark_order_dead("POLYMARKET")
    assert not liveness.venue_alive("POLYMARKET")


def test_all_alive_and_snapshot():
    liveness = VenueExecutionLiveness(("POLYMARKET", "ORBITEXCH"))
    for venue in ("POLYMARKET", "ORBITEXCH"):
        liveness.mark_order_alive(venue)
        liveness.mark_position_alive(venue)

    assert liveness.all_alive(("POLYMARKET", "ORBITEXCH"))
    assert liveness.snapshot()["POLYMARKET"] == {
        "order_alive": True,
        "position_alive": True,
        "venue_alive": True,
    }
