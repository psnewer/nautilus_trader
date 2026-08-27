# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
# -------------------------------------------------------------------------------------------------

"""二元 market 事件与上游源帧订单簿批次数据。"""

from __future__ import annotations

from nautilus_trader.core.data import Data
from nautilus_trader.model.data import DataType
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Venue


class MarketOrderBookDeltas(Data):
    """
    同一策略二元 market 在一条上游帧内的有效订单簿变化。

    ``deltas`` 只包含该帧实际变化的 instrument; 未出现的 market 成员继续使用
    DataEngine Cache 中的既有订单簿。批次不得为空, 也不得重复包含同一 instrument。
    """

    __slots__ = ("_ts_event", "_ts_init", "deltas", "market_id", "venue")

    def __init__(
        self,
        venue: Venue,
        market_id: str,
        deltas: tuple[OrderBookDeltas, ...],
        ts_event: int,
        ts_init: int,
    ) -> None:
        if not market_id:
            raise ValueError("market_id must not be empty")
        if not deltas:
            raise ValueError("deltas must not be empty")

        instrument_ids = tuple(item.instrument_id for item in deltas)
        if len(set(instrument_ids)) != len(instrument_ids):
            raise ValueError("deltas must contain each instrument at most once")
        if any(instrument_id.venue != venue for instrument_id in instrument_ids):
            raise ValueError("all deltas must belong to the batch venue")
        self.venue = venue
        self.market_id = market_id
        self.deltas = tuple(deltas)
        self._ts_event = int(ts_event)
        self._ts_init = int(ts_init)

    @property
    def ts_event(self) -> int:
        return self._ts_event

    @property
    def ts_init(self) -> int:
        return self._ts_init

    @property
    def instrument_ids(self) -> tuple[InstrumentId, ...]:
        return tuple(item.instrument_id for item in self.deltas)

    def __repr__(self) -> str:
        return (
            f"MarketOrderBookDeltas(venue={self.venue}, market_id={self.market_id!r}, "
            f"instruments={len(self.deltas)}, ts_event={self.ts_event}, ts_init={self.ts_init})"
        )


class OrderBookFrameDeltas(Data):
    """
    一条 venue 上游行情帧内的全部二元 market 变化。

    DataEngine 必须先应用全部 ``markets`` 的 inner OBD, 再逐个发布
    ``MarketOrderBookDeltas``; 因此源帧原子性与策略 market 事件边界互不混用。
    """

    __slots__ = ("_ts_event", "_ts_init", "frame_id", "markets", "source_market_id", "venue")

    def __init__(
        self,
        venue: Venue,
        source_market_id: str,
        markets: tuple[MarketOrderBookDeltas, ...],
        ts_event: int,
        ts_init: int,
        frame_id: int = 0,
    ) -> None:
        if not source_market_id:
            raise ValueError("source_market_id must not be empty")
        if not markets:
            raise ValueError("markets must not be empty")
        if any(market.venue != venue for market in markets):
            raise ValueError("all markets must belong to the frame venue")
        market_ids = tuple(market.market_id for market in markets)
        if len(set(market_ids)) != len(market_ids):
            raise ValueError("markets must contain each binary market at most once")
        instrument_ids = tuple(
            instrument_id for market in markets for instrument_id in market.instrument_ids
        )
        if len(set(instrument_ids)) != len(instrument_ids):
            raise ValueError("markets must not share instruments within one frame")
        if frame_id < 0:
            raise ValueError("frame_id must not be negative")
        self.venue = venue
        self.source_market_id = source_market_id
        self.markets = tuple(markets)
        self.frame_id = int(frame_id)
        self._ts_event = int(ts_event)
        self._ts_init = int(ts_init)

    @property
    def ts_event(self) -> int:
        return self._ts_event

    @property
    def ts_init(self) -> int:
        return self._ts_init

    def __repr__(self) -> str:
        return (
            f"OrderBookFrameDeltas(venue={self.venue}, source_market_id={self.source_market_id!r}, "
            f"markets={len(self.markets)}, frame_id={self.frame_id}, "
            f"ts_event={self.ts_event}, ts_init={self.ts_init})"
        )


class OrderBookFrameProcessed:
    """DataEngine 对一个带编号源帧的唯一处理终态。"""

    __slots__ = ("applied", "frame_id", "source_market_id", "venue")

    def __init__(
        self,
        venue: Venue,
        source_market_id: str,
        frame_id: int,
        applied: bool,
    ) -> None:
        if not source_market_id:
            raise ValueError("source_market_id must not be empty")
        if frame_id <= 0:
            raise ValueError("frame_id must be positive")
        self.venue = venue
        self.source_market_id = source_market_id
        self.frame_id = int(frame_id)
        self.applied = bool(applied)

    def __repr__(self) -> str:
        return (
            f"OrderBookFrameProcessed(venue={self.venue}, "
            f"source_market_id={self.source_market_id!r}, frame_id={self.frame_id}, "
            f"applied={self.applied})"
        )


def market_order_book_data_type(venue: Venue, market_id: str) -> DataType:
    """返回稳定的策略二元 market 订阅键。"""
    if not market_id:
        raise ValueError("market_id must not be empty")
    return DataType(
        MarketOrderBookDeltas,
        metadata={"venue": venue.value, "market_id": market_id},
    )


def order_book_frame_data_type(venue: Venue, source_market_id: str) -> DataType:
    """返回上游源帧的内部路由键。"""
    if not source_market_id:
        raise ValueError("source_market_id must not be empty")
    return DataType(
        OrderBookFrameDeltas,
        metadata={"venue": venue.value, "source_market_id": source_market_id},
    )


def order_book_frame_processed_topic(venue: Venue) -> str:
    """返回 DataEngine 源帧处理完成通知的 venue 级内部 topic。"""
    return f"data.order_book_frame.processed.{venue.value}"
