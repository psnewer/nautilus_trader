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
"""
Per-order fill tracking with dust detection for the Polymarket adapter.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from nautilus_trader.adapters.polymarket.common.constants import DUST_SNAP_THRESHOLD
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Quantity


CAPACITY = 10_000


@dataclass
class _OrderFillState:
    submitted_qty: Quantity
    cumulative_filled: float
    last_fill_px: float
    last_fill_ts: int
    order_side: OrderSide
    instrument_id: InstrumentId
    size_precision: int
    price_precision: int


class OrderFillTracker:
    """
    Tracks per-order fill accumulation.

    Dust snapping / synthetic dust *fills* are disabled (see `snap_fill_qty` and
    `check_dust_residual`): reported fills always equal actual venue fills, so a
    reduce SELL can never oversell into a dust SHORT on a probability venue.
    """

    def __init__(self) -> None:
        self._orders: OrderedDict[VenueOrderId, _OrderFillState] = OrderedDict()

    def register(
        self,
        venue_order_id: VenueOrderId,
        submitted_qty: Quantity,
        order_side: OrderSide,
        instrument_id: InstrumentId,
        size_precision: int,
        price_precision: int,
    ) -> None:
        """
        Register an order after HTTP accept.
        """
        state = _OrderFillState(
            submitted_qty=submitted_qty,
            cumulative_filled=0.0,
            last_fill_px=0.0,
            last_fill_ts=0,
            order_side=order_side,
            instrument_id=instrument_id,
            size_precision=size_precision,
            price_precision=price_precision,
        )
        self._orders[venue_order_id] = state
        # Evict oldest if over capacity
        while len(self._orders) > CAPACITY:
            self._orders.popitem(last=False)

    def contains(self, venue_order_id: VenueOrderId) -> bool:
        """
        Return true if the order has been registered.
        """
        return venue_order_id in self._orders

    def snap_fill_qty(self, venue_order_id: VenueOrderId, fill_qty: Quantity) -> Quantity:
        """
        Return the fill qty unchanged — dust snapping is disabled (both sides).

        On Polymarket (a probability venue) SELLs are reduce-only. Snapping a fill
        UP to the submitted qty oversells the long into a tiny dust SHORT, which
        trips the "position cannot be SHORT" invariant. Snapping a BUY up merely
        manufactures a dust phantom LONG. Either way the actual venue fill is the
        truth; any residual leaves is settled by the residual-cancel path, never by
        inflating a reported fill.
        """
        return fill_qty

    def record_fill(
        self,
        venue_order_id: VenueOrderId,
        qty: float,
        px: float,
        ts: int,
    ) -> None:
        """
        Record a fill, updating cumulative total and last price/ts.
        """
        state = self._orders.get(venue_order_id)
        if state is not None:
            state.cumulative_filled += qty
            state.last_fill_px = px
            state.last_fill_ts = ts

    def check_dust_residual(
        self,
        venue_order_id: VenueOrderId,
    ) -> Quantity | None:
        """
        Return the un-filled dust residual (< DUST_SNAP_THRESHOLD), else None.

        **Detection only — no side effect, no synthetic fill.** When the venue reports
        the order terminal (order UPDATE/MATCHED, or a cancel rejected as "already
        canceled or matched") but a tiny remainder never filled and the venue dropped
        it, the caller closes the order via `generate_order_canceled` — a terminal
        event that keeps `filled_qty` at what actually filled and **does not move the
        position**. That is deliberately NOT a synthetic fill: filling the residual
        would over-sell a reduce SELL into a dust SHORT (probability-venue invariant)
        or manufacture a phantom LONG on a BUY. Symmetric for both sides.
        """
        state = self._orders.get(venue_order_id)
        if state is None:
            return None
        leaves = state.submitted_qty.as_double() - state.cumulative_filled
        if 0.0 < leaves < DUST_SNAP_THRESHOLD:
            return Quantity(leaves, state.size_precision)
        return None
