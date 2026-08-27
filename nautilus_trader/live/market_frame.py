# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
# -------------------------------------------------------------------------------------------------

"""Live DataClient 的订单簿源帧 single-flight 合并。"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field

from nautilus_trader.model.data import CustomData
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.market_order_book import MarketOrderBookDeltas
from nautilus_trader.model.market_order_book import OrderBookFrameDeltas
from nautilus_trader.model.market_order_book import OrderBookFrameProcessed
from nautilus_trader.model.market_order_book import order_book_frame_data_type


@dataclass(slots=True)
class _PendingFrame:
    venue: Venue
    source_market_id: str
    markets: dict[str, dict[InstrumentId, OrderBookDeltas]]
    ts_event: int
    ts_init: int
    barrier: bool = False

    @classmethod
    def from_frame(cls, frame: OrderBookFrameDeltas, *, barrier: bool) -> _PendingFrame:
        markets: dict[str, dict[InstrumentId, OrderBookDeltas]] = {}
        pending = cls(
            venue=frame.venue,
            source_market_id=frame.source_market_id,
            markets=markets,
            ts_event=frame.ts_event,
            ts_init=frame.ts_init,
            barrier=barrier,
        )
        pending.merge(frame)
        return pending

    def merge(self, frame: OrderBookFrameDeltas) -> None:
        if frame.venue != self.venue or frame.source_market_id != self.source_market_id:
            raise ValueError("cannot merge frames from different venue/source markets")
        for market in frame.markets:
            by_instrument = self.markets.setdefault(market.market_id, {})
            for deltas in market.deltas:
                if not deltas.deltas or deltas.deltas[0].action != BookAction.CLEAR:
                    raise ValueError(
                        "single-flight market frames require a full instrument snapshot starting with CLEAR",
                    )
                by_instrument[deltas.instrument_id] = deltas
        self.ts_event = max(self.ts_event, frame.ts_event)
        self.ts_init = max(self.ts_init, frame.ts_init)

    def build(self, frame_id: int) -> OrderBookFrameDeltas:
        markets = tuple(
            MarketOrderBookDeltas(
                venue=self.venue,
                market_id=market_id,
                deltas=tuple(by_instrument.values()),
                ts_event=max(item.ts_event for item in by_instrument.values()),
                ts_init=max(item.ts_init for item in by_instrument.values()),
            )
            for market_id, by_instrument in sorted(self.markets.items())
        )
        return OrderBookFrameDeltas(
            venue=self.venue,
            source_market_id=self.source_market_id,
            markets=markets,
            ts_event=self.ts_event,
            ts_init=self.ts_init,
            frame_id=frame_id,
        )


@dataclass(slots=True)
class _SourceState:
    in_flight_frame_id: int | None = None
    pending: deque[_PendingFrame] = field(default_factory=deque)
    blocked: bool = False


class MarketFrameConflater:
    """保证每个 source market 最多只有一个 frame 已投递至 DataEngine。"""

    def __init__(
        self,
        venue: Venue,
        publish: Callable[[CustomData], None],
    ) -> None:
        self._venue = venue
        self._publish = publish
        self._states: dict[str, _SourceState] = {}
        self._next_frame_id = 1

    def activate(self, source_market_id: str) -> None:
        """激活 source;重复订阅同一 source 不重置已有在飞状态。"""
        self._states.setdefault(source_market_id, _SourceState())

    def deactivate(self, source_market_id: str) -> None:
        """退订 source,并使其全部迟到 completion 失效。"""
        self._states.pop(source_market_id, None)

    def offer(self, frame: OrderBookFrameDeltas, *, barrier: bool = False) -> bool:
        """投递或合并完整 snapshot;返回本次是否立即投递。"""
        if frame.venue != self._venue:
            raise ValueError(f"frame venue {frame.venue} does not match conflater venue {self._venue}")
        state = self._states.setdefault(frame.source_market_id, _SourceState())
        if state.blocked:
            return False
        pending = _PendingFrame.from_frame(frame, barrier=barrier)
        if state.in_flight_frame_id is None:
            self._send(state, pending)
            return True
        if barrier or not state.pending or state.pending[-1].barrier:
            state.pending.append(pending)
        else:
            state.pending[-1].merge(frame)
        return False

    def on_processed(self, processed: OrderBookFrameProcessed) -> bool:
        """消费 DataEngine 终态;返回是否接受了当前在飞 frame 的 completion。"""
        if processed.venue != self._venue:
            return False
        state = self._states.get(processed.source_market_id)
        if state is None or state.in_flight_frame_id != processed.frame_id:
            return False
        state.in_flight_frame_id = None
        if not processed.applied:
            state.pending.clear()
            state.blocked = True
            return True
        if state.pending:
            self._send(state, state.pending.popleft())
        return True

    def _send(self, state: _SourceState, pending: _PendingFrame) -> None:
        frame_id = self._next_frame_id
        self._next_frame_id += 1
        state.in_flight_frame_id = frame_id
        frame = pending.build(frame_id)
        try:
            self._publish(
                CustomData(
                    order_book_frame_data_type(frame.venue, frame.source_market_id),
                    frame,
                ),
            )
        except Exception:
            state.in_flight_frame_id = None
            state.pending.clear()
            state.blocked = True
            raise
