"""Venue market 级订单簿订阅的应用层接线。"""

from __future__ import annotations

from dataclasses import dataclass

from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_condition_id
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.market_order_book import market_order_book_data_type
from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.common.venues import venue_id_from_instrument_id


@dataclass(frozen=True, slots=True)
class MarketBookSubscription:
    venue: Venue
    market_id: str
    source_market_id: str
    instrument_ids: tuple[InstrumentId, ...]
    game_id: int | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.venue.value, self.market_id

    @property
    def data_type(self):
        return market_order_book_data_type(self.venue, self.market_id)

    @property
    def params(self) -> dict:
        params = {
            "instrument_ids": self.instrument_ids,
            "source_market_id": self.source_market_id,
            "managed": True,
            "book_type": BookType.L2_MBP,
        }
        if self.game_id is not None:
            params["game_id"] = self.game_id
        return params


def market_book_subscriptions(
    cache,
    instrument_ids,
    *,
    game_id: int | None = None,
) -> tuple[MarketBookSubscription, ...]:
    """把真实 instrument 按 ``(venue, binary_market_id)`` 分组。"""
    grouped: dict[tuple[str, str, str], list[InstrumentId]] = {}
    for value in instrument_ids:
        instrument_id = (
            value if isinstance(value, InstrumentId) else InstrumentId.from_str(str(value))
        )
        instrument = cache.instrument(instrument_id)
        if instrument is None:
            raise ValueError(f"Instrument {instrument_id} is not in cache")
        venue_value = venue_id_from_instrument_id(instrument_id)
        source_market_id = (
            get_polymarket_condition_id(instrument_id)
            if venue_value == POLYMARKET
            else str(getattr(instrument, "market_id", "") or "")
        )
        info = getattr(instrument, "info", None) or {}
        market_id = (
            source_market_id
            if venue_value == POLYMARKET
            else str(info.get("binary_market_id") or "")
        )
        if not source_market_id:
            raise ValueError(f"Instrument {instrument_id} has no venue market_id")
        if not market_id:
            raise ValueError(f"Instrument {instrument_id} has no binary_market_id")
        grouped.setdefault((instrument_id.venue.value, market_id, source_market_id), []).append(
            instrument_id,
        )
    return tuple(
        MarketBookSubscription(
            Venue(venue),
            market_id,
            source_market_id,
            tuple(sorted(members, key=str)),
            game_id,
        )
        for (venue, market_id, source_market_id), members in sorted(grouped.items())
    )


def subscribe_market_book(
    actor, subscription: MarketBookSubscription, *, managed: bool = True
) -> None:
    params = subscription.params
    params["managed"] = managed
    actor.subscribe_data(
        subscription.data_type,
        client_id=ClientId(subscription.venue.value),
        params=params,
    )


def unsubscribe_market_book(actor, subscription: MarketBookSubscription) -> None:
    actor.unsubscribe_data(
        subscription.data_type,
        client_id=ClientId(subscription.venue.value),
        params=subscription.params,
    )


def market_book_topic(subscription: MarketBookSubscription) -> str:
    return f"data.{subscription.data_type.topic}"
