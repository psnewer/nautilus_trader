# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------

from nautilus_trader.live.market_frame import MarketFrameConflater
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.market_order_book import MarketOrderBookDeltas
from nautilus_trader.model.market_order_book import OrderBookFrameDeltas
from nautilus_trader.model.market_order_book import OrderBookFrameProcessed
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


VENUE = Venue("POLYMARKET")
YES = InstrumentId.from_str("YES.POLYMARKET")
NO = InstrumentId.from_str("NO.POLYMARKET")


def _snapshot(instrument_id: InstrumentId, price: str, ts: int) -> OrderBookDeltas:
    return OrderBookDeltas(
        instrument_id,
        [
            OrderBookDelta.clear(instrument_id, 0, ts, ts),
            OrderBookDelta(
                instrument_id=instrument_id,
                action=BookAction.ADD,
                order=BookOrder(
                    OrderSide.SELL,
                    Price.from_str(price),
                    Quantity.from_int(1),
                    1,
                ),
                flags=0,
                sequence=1,
                ts_event=ts,
                ts_init=ts,
            ),
        ],
    )


def _frame(*items: tuple[str, OrderBookDeltas], ts: int = 1) -> OrderBookFrameDeltas:
    grouped: dict[str, list[OrderBookDeltas]] = {}
    for market_id, deltas in items:
        grouped.setdefault(market_id, []).append(deltas)
    return OrderBookFrameDeltas(
        venue=VENUE,
        source_market_id="source",
        markets=tuple(
            MarketOrderBookDeltas(VENUE, market_id, tuple(deltas), ts, ts)
            for market_id, deltas in grouped.items()
        ),
        ts_event=ts,
        ts_init=ts,
    )


def _complete(frame, *, applied: bool = True) -> OrderBookFrameProcessed:
    return OrderBookFrameProcessed(VENUE, "source", frame.frame_id, applied)


def test_conflater_keeps_one_in_flight_and_replaces_pending_instrument_snapshot() -> None:
    published = []
    conflater = MarketFrameConflater(VENUE, published.append)

    assert conflater.offer(_frame(("market", _snapshot(YES, "0.40", 1)), ts=1))
    assert not conflater.offer(_frame(("market", _snapshot(YES, "0.41", 2)), ts=2))
    assert not conflater.offer(_frame(("market", _snapshot(YES, "0.42", 3)), ts=3))
    assert len(published) == 1

    assert conflater.on_processed(_complete(published[0].data))
    assert len(published) == 2
    assert published[1].data.markets[0].deltas[0].deltas[1].order.price == Price.from_str("0.42")


def test_conflater_rebuilds_multiple_binary_markets() -> None:
    published = []
    conflater = MarketFrameConflater(VENUE, published.append)
    conflater.offer(_frame(("home", _snapshot(YES, "0.40", 1)), ts=1))
    conflater.offer(_frame(("draw", _snapshot(NO, "0.55", 2)), ts=2))

    conflater.on_processed(_complete(published[0].data))

    assert [market.market_id for market in published[1].data.markets] == ["draw"]
    assert published[1].data.markets[0].instrument_ids == (NO,)


def test_conflater_preserves_clear_barrier_before_later_snapshot() -> None:
    published = []
    conflater = MarketFrameConflater(VENUE, published.append)
    conflater.offer(_frame(("market", _snapshot(YES, "0.40", 1)), ts=1))
    clear = OrderBookDeltas(YES, [OrderBookDelta.clear(YES, 0, 2, 2)])
    conflater.offer(_frame(("market", clear), ts=2), barrier=True)
    conflater.offer(_frame(("market", _snapshot(YES, "0.45", 3)), ts=3))

    conflater.on_processed(_complete(published[0].data))
    assert len(published) == 2
    assert len(published[1].data.markets[0].deltas[0].deltas) == 1
    conflater.on_processed(_complete(published[1].data))
    assert len(published) == 3
    assert published[2].data.markets[0].deltas[0].deltas[1].order.price == Price.from_str("0.45")


def test_conflater_ignores_late_completion_after_deactivate() -> None:
    published = []
    conflater = MarketFrameConflater(VENUE, published.append)
    conflater.offer(_frame(("market", _snapshot(YES, "0.40", 1)), ts=1))
    old = published[0].data
    conflater.deactivate("source")
    conflater.activate("source")

    assert not conflater.on_processed(_complete(old))


def test_conflater_blocks_source_after_rejected_frame_until_reactivated() -> None:
    published = []
    conflater = MarketFrameConflater(VENUE, published.append)
    frame = _frame(("market", _snapshot(YES, "0.40", 1)), ts=1)
    conflater.offer(frame)
    conflater.offer(_frame(("market", _snapshot(YES, "0.41", 2)), ts=2))

    assert conflater.on_processed(_complete(published[0].data, applied=False))
    assert not conflater.offer(_frame(("market", _snapshot(YES, "0.42", 3)), ts=3))
    assert len(published) == 1

    conflater.deactivate("source")
    conflater.activate("source")
    assert conflater.offer(frame)
    assert len(published) == 2
