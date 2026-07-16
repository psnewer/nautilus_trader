"""Risk 测试用的轻量构造器:带 info 的 PM/OE/SE instrument、duck position、账户状态。"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import BettingInstrument
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.instruments.betting import null_handicap
from nautilus_trader.model.objects import AccountBalance
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


def pm_instrument(competition: str, market_type: str, token: str = "tok1") -> BinaryOption:
    """POLYMARKET BinaryOption,info 携带 competition/market_type(discovery 契约)。"""
    raw = Symbol(f"0xcond-{token}")
    return BinaryOption(
        instrument_id=InstrumentId(symbol=raw, venue=Venue("POLYMARKET")),
        raw_symbol=raw,
        outcome="Yes",
        description="test",
        asset_class=AssetClass.ALTERNATIVE,
        currency=USDC,
        price_precision=3,
        price_increment=Price.from_str("0.001"),
        size_precision=2,
        size_increment=Quantity.from_str("0.01"),
        activation_ns=0,
        expiration_ns=pd.Timestamp("2030-01-01", tz="UTC").value,
        max_quantity=None,
        min_quantity=Quantity.from_int(5),
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        ts_event=0,
        ts_init=0,
        info={"competition": competition, "market_type": market_type, "min_buy_notional": 1.0},
    )


def _betting_instrument(
    venue_name: str,
    competition: str,
    market_type: str,
    selection_id: int = 1,
) -> BettingInstrument:
    return BettingInstrument(
        venue_name=venue_name,
        betting_type="ODDS",
        competition_id=1,
        competition_name=competition,
        event_country_code="GB",
        event_id=1,
        event_name="evt",
        event_open_date=pd.Timestamp("2030-02-07 23:30:00+00:00"),
        event_type_id=1,
        event_type_name="Soccer",
        market_id="1-123",
        market_name="Match Odds",
        market_start_time=pd.Timestamp("2030-02-07 23:30:00+00:00"),
        market_type="MATCH_ODDS",
        selection_handicap=null_handicap(),
        selection_id=selection_id,
        selection_name=market_type,
        currency="USD",
        price_precision=2,
        size_precision=2,
        min_notional=Money(1, USD),
        ts_event=0,
        ts_init=0,
        info={"competition": competition, "market_type": market_type},
    )


def oe_instrument(competition: str, market_type: str, selection_id: int = 1) -> BettingInstrument:
    """ORBITEXCH BettingInstrument,info 携带 competition/market_type。"""
    return _betting_instrument("ORBITEXCH", competition, market_type, selection_id)


def se_instrument(competition: str, market_type: str, selection_id: int = 1) -> BettingInstrument:
    """SHARPEXCH BettingInstrument,info 携带 competition/market_type。"""
    return _betting_instrument("SHARPEXCH", competition, market_type, selection_id)


class DuckPosition:
    """只暴露 ArbitragePortfolio._leg_from_position / _resolve_pair_id 触及的字段。"""

    def __init__(
        self,
        instrument_id: InstrumentId,
        qty: float,
        avg_px: float,
        side: PositionSide = PositionSide.LONG,
    ) -> None:
        self.instrument_id = instrument_id
        self.quantity = Quantity(qty, 2)
        self.avg_px_open = avg_px
        self.side = side


def pm_account_state(total: float, account_id: str = "POLYMARKET-001") -> AccountState:
    bal = AccountBalance(Money(total, USDC), Money(0, USDC), Money(total, USDC))
    return _cash_state(account_id, [bal])


def oe_account_state(total: float, free: float, account_id: str = "ORBITEXCH-001") -> AccountState:
    locked = total - free
    bal = AccountBalance(Money(total, USD), Money(locked, USD), Money(free, USD))
    return _cash_state(account_id, [bal])


def se_account_state(total: float, free: float, account_id: str = "SHARPEXCH-001") -> AccountState:
    locked = total - free
    bal = AccountBalance(Money(total, USD), Money(locked, USD), Money(free, USD))
    return _cash_state(account_id, [bal])


def _cash_state(account_id: str, balances: list[AccountBalance]) -> AccountState:
    return AccountState(
        account_id=AccountId(account_id),
        account_type=AccountType.CASH,
        base_currency=None,  # 多币种/单币种均按 currency 查询
        reported=True,
        balances=balances,
        margins=[],
        info={},
        event_id=UUID4(),
        ts_event=0,
        ts_init=0,
    )
