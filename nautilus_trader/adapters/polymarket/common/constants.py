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

from typing import Final

from nautilus_trader.adapters.polymarket.common.enums import PolymarketTradeStatus
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import Venue


POLYMARKET: Final[str] = "POLYMARKET"
POLYMARKET_VENUE: Final[Venue] = Venue(POLYMARKET)
POLYMARKET_CLIENT_ID: Final[ClientId] = ClientId(POLYMARKET)

POLYMARKET_MAX_PRICE: Final[float] = 0.999
POLYMARKET_MIN_PRICE: Final[float] = 0.001
POLYMARKET_MAX_PRECISION_TAKER: Final[int] = 2
POLYMARKET_MAX_PRECISION_MAKER: Final[int] = 5

VALID_POLYMARKET_TIME_IN_FORCE: Final[set[TimeInForce]] = {
    TimeInForce.GTC,
    TimeInForce.GTD,
    TimeInForce.FOK,
    TimeInForce.IOC,
}

POLYMARKET_INVALID_API_KEY: Final[str] = "Unauthorized/Invalid api key"
POLYMARKET_CANCEL_ALREADY_DONE: Final[str] = "already canceled or matched"

POLYMARKET_FINALIZED_TRADE_STATUSES: Final[tuple[PolymarketTradeStatus, ...]] = (
    PolymarketTradeStatus.CONFIRMED,
)

POLYMARKET_HTTP_RATE_LIMIT: Final[int] = 100  # requests per minute

# Un-filled leaves below this are venue matching-error dust: the order is terminalized
# via cancel (position kept), so a sub-fillable reduce remainder doesn't linger as a
# stuck residual (cancel-only → already-canceled-or-matched → watchdog timeout loop).
# 0.02-class residuals were slipping through at 0.01; 0.1 covers them (0.5 is too coarse).
DUST_SNAP_THRESHOLD: Final[float] = 0.1
