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

from __future__ import annotations

import hashlib
import time
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any

import pandas as pd

from nautilus_trader.adapters.polymarket.common.enums import PolymarketLiquiditySide
from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderSide
from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderStatus
from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderType
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_instrument_id
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
from nautilus_trader.model.currencies import USDC_POS
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


if TYPE_CHECKING:
    from nautilus_trader.adapters.polymarket.schemas.book import PolymarketTickSizeChange


def determine_trade_id(
    asset_id: str,
    side: PolymarketOrderSide,
    price: str,
    size: str,
    timestamp: str,
) -> TradeId:
    """
    Derive a deterministic `TradeId` for a Polymarket market data trade.

    Polymarket does not publish a trade ID with `last_trade_price` events, so we
    derive one from the trade's identifying fields. Using blake2b with a 0x1f
    delimiter prevents variable-length fields from colliding (e.g. "0.12" + "34"
    vs "0.1" + "234").

    Parameters
    ----------
    asset_id : str
        The Polymarket asset (token) ID.
    side : PolymarketOrderSide
        The aggressor side of the trade.
    price : str
        The trade price as sent by the venue.
    size : str
        The trade size as sent by the venue.
    timestamp : str
        The trade timestamp as sent by the venue (milliseconds since epoch).

    Returns
    -------
    TradeId

    """
    side_byte = b"B" if side == PolymarketOrderSide.BUY else b"S"
    digest = hashlib.blake2b(digest_size=8)
    digest.update(
        b"\x1f".join(
            (
                asset_id.encode(),
                side_byte,
                price.encode(),
                size.encode(),
                timestamp.encode(),
            ),
        ),
    )
    return TradeId(digest.hexdigest())


def make_composite_trade_id(trade_id: str, venue_order_id: VenueOrderId) -> TradeId:
    """
    Create a composite trade_id to ensure uniqueness across multi-order fills.

    When multiple orders are filled by a single market order, Polymarket sends one
    TRADE message with a single `id` for all fills. This function creates a unique
    trade_id for each fill by combining the original trade ID with part of the
    venue order ID.

    Format: {trade_id[:27]}-{venue_order_id[-8:]} = 36 chars (TradeId max length)

    """
    return TradeId(f"{trade_id[:27]}-{str(venue_order_id)[-8:]}")


def validate_ethereum_address(address: str) -> None:
    if not address.startswith("0x") or len(address) != 42:
        raise ValueError(
            f"Invalid Ethereum address format: {address!r}. "
            f"Expected 0x prefix with 40 hexadecimal characters",
        )
    try:
        int(address[2:], 16)
    except ValueError as e:
        raise ValueError(
            f"Invalid Ethereum address format: {address!r}. "
            f"Address contains non-hexadecimal characters",
        ) from e


def parse_order_side(order_side: PolymarketOrderSide) -> OrderSide:
    match order_side:
        case PolymarketOrderSide.BUY:
            return OrderSide.BUY
        case PolymarketOrderSide.SELL:
            return OrderSide.SELL
        case _:
            # Theoretically unreachable but retained to keep the match exhaustive
            raise ValueError(f"invalid order side, was {order_side}")


def determine_order_side(
    trader_side: PolymarketLiquiditySide,
    trade_side: PolymarketOrderSide,
    taker_asset_id: str,
    maker_asset_id: str,
) -> OrderSide:
    """
    Determine the order side for a fill based on trader role and asset matching.

    Polymarket uses a unified order book where complementary tokens (YES/NO) can match
    across assets. This means a BUY YES can match with a BUY NO (cross-asset), not just
    with a SELL YES (same-asset).

    """
    order_side = parse_order_side(trade_side)

    if trader_side == PolymarketLiquiditySide.TAKER:
        return order_side

    # For MAKER: determine side based on whether assets match
    is_cross_asset = maker_asset_id != taker_asset_id

    if is_cross_asset:
        # Cross-asset match: both sides are the same
        return order_side
    else:
        # Same-asset match: sides are opposite
        return OrderSide.BUY if order_side == OrderSide.SELL else OrderSide.SELL


def parse_liquidity_side(liquidity_side: PolymarketLiquiditySide) -> LiquiditySide:
    match liquidity_side:
        case PolymarketLiquiditySide.MAKER:
            return LiquiditySide.MAKER
        case PolymarketLiquiditySide.TAKER:
            return LiquiditySide.TAKER
        case _:
            # Theoretically unreachable but retained to keep the match exhaustive
            raise ValueError(f"invalid liquidity side, was {liquidity_side}")


def parse_time_in_force(order_type: PolymarketOrderType) -> TimeInForce:
    match order_type:
        case PolymarketOrderType.GTC:
            return TimeInForce.GTC
        case PolymarketOrderType.GTD:
            return TimeInForce.GTD
        case PolymarketOrderType.FOK:
            return TimeInForce.FOK
        case PolymarketOrderType.FAK:
            return TimeInForce.IOC
        case _:
            # Theoretically unreachable but retained to keep the match exhaustive
            raise ValueError(f"invalid order type, was {order_type}")


def parse_order_status(order_status: PolymarketOrderStatus) -> OrderStatus:
    match order_status:
        case PolymarketOrderStatus.INVALID | PolymarketOrderStatus.UNMATCHED:
            return OrderStatus.REJECTED
        case PolymarketOrderStatus.LIVE | PolymarketOrderStatus.DELAYED:
            return OrderStatus.ACCEPTED
        case PolymarketOrderStatus.CANCELED | PolymarketOrderStatus.CANCELED_MARKET_RESOLVED:
            return OrderStatus.CANCELED
        case PolymarketOrderStatus.MATCHED:
            return OrderStatus.FILLED


def parse_polymarket_instrument(
    market_info: dict[str, Any],
    token_id: str,
    outcome: str,
    ts_init: int | None = None,
) -> BinaryOption:
    instrument_id = get_polymarket_instrument_id(str(market_info["condition_id"]), token_id)
    raw_symbol = Symbol(get_polymarket_token_id(instrument_id))
    description = market_info["question"]
    price_increment = Price.from_str(str(market_info["minimum_tick_size"]))
    min_quantity = Quantity.from_str(str(market_info.get("minimum_order_size", 5)))
    # PM 下单量可撮粒度(#281):真实增量是 0.01(个别市场 0.001)。此前硬编码 USDC.e 的 0.000001
    # (精度 6),把"成交报表记账精度"误当"下单可撮粒度",导致下单量带 6 位小数(如 28.7525)、venue
    # 只撮到 0.01、余下 sub-0.01 尾量无法挂单成交 → 订单卡 PARTIALLY_FILLED(#280 dust 根源)。
    # 改用 0.01:它是任何更细真实增量(0.001)的整数倍,发 venue 永远合法,且把下单量量化到 0.01
    # → 从源头不再产生 sub-0.01 dust。#280 的 fill-handler cancel 收口作为残余兜底保留。
    size_increment = Quantity.from_str("0.01")
    end_date_iso = market_info["end_date_iso"]

    if end_date_iso:
        expiration_ns = pd.Timestamp(end_date_iso).value
    else:
        # end_date_iso can be missing in some conditions that are part of an event that has it
        expiration_ns = (pd.Timestamp.now(tz="UTC") + pd.DateOffset(years=10)).value

    maker_fee, taker_fee = extract_fee_rates(market_info)
    instrument_info = dict(market_info)
    # PM 的 1 USD 下限只适用于 BUY；BinaryOption.min_notional 无法表达侧别约束。
    instrument_info.setdefault("min_buy_notional", 1.0)

    ts_init = ts_init if ts_init is not None else time.time_ns()

    return BinaryOption(
        instrument_id=instrument_id,
        raw_symbol=raw_symbol,
        outcome=outcome,
        description=description,
        asset_class=AssetClass.ALTERNATIVE,
        currency=USDC_POS,
        price_increment=price_increment,
        price_precision=price_increment.precision,
        size_increment=size_increment,
        size_precision=size_increment.precision,
        activation_ns=0,  # TBD?
        expiration_ns=expiration_ns,
        max_quantity=None,
        min_quantity=min_quantity,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        ts_event=ts_init,
        ts_init=ts_init,
        info=instrument_info,
    )


def update_instrument(
    instrument: BinaryOption,
    change: PolymarketTickSizeChange,
    ts_init: int,
) -> BinaryOption:
    price_increment = Price.from_str(change.new_tick_size)

    return BinaryOption(
        instrument_id=instrument.id,
        raw_symbol=instrument.raw_symbol,
        outcome=instrument.outcome,
        description=instrument.description,
        asset_class=AssetClass.ALTERNATIVE,
        currency=USDC_POS,
        price_increment=price_increment,
        price_precision=price_increment.precision,
        size_increment=instrument.size_increment,
        size_precision=instrument.size_precision,
        activation_ns=instrument.activation_ns,
        expiration_ns=instrument.expiration_ns,
        max_quantity=None,
        min_quantity=instrument.min_quantity,
        maker_fee=instrument.maker_fee,
        taker_fee=instrument.taker_fee,
        ts_event=ts_init,
        ts_init=ts_init,
        info=instrument.info,
    )


def basis_points_as_decimal(basis_points: Decimal) -> Decimal:
    """
    Convert basis points to a decimal fraction.

    Parameters
    ----------
    basis_points : Decimal
        The fee rate in basis points (1 bp = 0.01%).

    Returns
    -------
    Decimal
        The decimal fraction (e.g., 100 bp -> 0.01).

    """
    return basis_points / Decimal(10_000)


def extract_fee_rates(market_info: dict[str, Any]) -> tuple[Decimal, Decimal]:
    """
    Extract effective maker and taker fee rates from Polymarket market info.

    Polymarket charges fees using the `feeSchedule.rate` from the Gamma market
    data. Only takers pay fees, so the maker rate is always zero. When the
    feeSchedule is not present (e.g. CLOB-only flow), both rates default to
    zero since there is no reliable source for the effective rate.

    The `maker_base_fee` and `taker_base_fee` fields represent the maximum
    fee cap used when signing orders (`fee_rate_bps`), not the effective fee
    charged at settlement. See Polymarket docs for details.

    Parameters
    ----------
    market_info : dict[str, Any]
        The Polymarket market info dictionary (CLOB or normalized Gamma format).

    Returns
    -------
    tuple[Decimal, Decimal]
        A tuple of (maker_fee, taker_fee) as decimal fractions.

    References
    ----------
    https://docs.polymarket.com/trading/fees

    """
    fee_schedule = market_info.get("feeSchedule")
    if fee_schedule is None:
        gamma_original = market_info.get("_gamma_original") or {}
        fee_schedule = gamma_original.get("feeSchedule")

    if fee_schedule is None:
        return Decimal(0), Decimal(0)

    rate = fee_schedule.get("rate")
    if rate is None:
        return Decimal(0), Decimal(0)

    taker_fee = Decimal(str(rate))
    return Decimal(0), taker_fee


def calculate_commission(
    quantity: Decimal,
    price: Decimal,
    fee_rate: Decimal,
    liquidity_side: LiquiditySide,
) -> float:
    """
    Calculate the Polymarket commission for a fill.

    Polymarket fees follow the formula `fee = C * feeRate * p * (1 - p)`,
    where C is the number of shares, feeRate is the effective taker rate from
    the market's feeSchedule, and p is the share price. Fees peak at p=0.5
    and decrease symmetrically toward both extremes. Only takers pay fees;
    makers are always charged zero.

    Fees are rounded to 5 decimal places (the smallest charged fee is
    0.00001 USDC).

    Parameters
    ----------
    quantity : Decimal
        The fill quantity (shares).
    price : Decimal
        The fill price.
    fee_rate : Decimal
        The effective fee rate as a decimal fraction (e.g., 0.03 for 3%).
    liquidity_side : LiquiditySide
        The liquidity side for this fill.

    Returns
    -------
    float
        The commission amount in USDC rounded to 5 decimal places.

    References
    ----------
    https://docs.polymarket.com/trading/fees

    """
    if liquidity_side != LiquiditySide.TAKER or fee_rate == 0:
        return 0.0

    commission = quantity * price * fee_rate * (Decimal(1) - price)
    return round(float(commission), 5)
