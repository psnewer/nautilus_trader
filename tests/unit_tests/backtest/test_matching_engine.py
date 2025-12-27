# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
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

from typing import Any

import pytest

from nautilus_trader.backtest.engine import OrderMatchingEngine
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.backtest.models import MakerTakerFeeModel
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import TestClock
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import ModifyOrder
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import InstrumentCloseType
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.enums import MarketStatusAction
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.events import OrderModifyRejected
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import MarketOrder
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.data import TestDataStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs


_ETHUSDT_PERP_BINANCE = TestInstrumentProvider.ethusdt_perp_binance()


class TestOrderMatchingEngine:
    def setup(self):
        # Fixture Setup
        self.clock = TestClock()
        self.trader_id = TestIdStubs.trader_id()

        self.msgbus = MessageBus(
            trader_id=self.trader_id,
            clock=self.clock,
        )
        self.instrument = _ETHUSDT_PERP_BINANCE
        self.instrument_id = self.instrument.id
        self.account_id = TestIdStubs.account_id()
        self.cache = TestComponentStubs.cache()
        self.cache.add_instrument(self.instrument)

        self.matching_engine = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L1_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            trade_execution=True,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )

    def test_repr(self) -> None:
        # Arrange, Act, Assert
        assert (
            repr(self.matching_engine)
            == "OrderMatchingEngine(venue=BINANCE, instrument_id=ETHUSDT-PERP.BINANCE, raw_id=0)"
        )

    def test_set_fill_model(self) -> None:
        # Arrange
        fill_model = FillModel()

        # Act
        self.matching_engine.set_fill_model(fill_model)

        # Assert
        assert True

    def test_update_instrument(self) -> None:
        # Arrange, Act
        self.matching_engine.update_instrument(_ETHUSDT_PERP_BINANCE)

        # Assert
        assert self.matching_engine.instrument.id == _ETHUSDT_PERP_BINANCE.id

    def test_process_instrument_status(self) -> None:
        self.matching_engine.process_status(MarketStatusAction.CLOSE)
        self.matching_engine.process_status(MarketStatusAction.PRE_OPEN)
        self.matching_engine.process_status(MarketStatusAction.PAUSE)
        self.matching_engine.process_status(MarketStatusAction.TRADING)

    def test_process_market_on_close_order(self) -> None:
        order: MarketOrder = TestExecStubs.market_order(
            instrument=self.instrument,
            time_in_force=TimeInForce.AT_THE_CLOSE,
        )
        self.matching_engine.process_order(order, self.account_id)

    def test_instrument_close_expiry_closes_position(self) -> None:
        # Arrange
        exec_messages = []
        self.msgbus.register("ExecEngine.process", lambda x: exec_messages.append(x))
        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
        )
        self.matching_engine.process_quote_tick(quote)
        order: MarketOrder = TestExecStubs.limit_order(
            instrument=self.instrument,
        )
        self.matching_engine.process_order(order, self.account_id)

        # Act
        instrument_close = TestDataStubs.instrument_close(
            instrument_id=self.instrument_id,
            price=Price.from_str("2.00"),
            close_type=InstrumentCloseType.CONTRACT_EXPIRED,
            ts_event=2,
        )
        self.matching_engine.process_instrument_close(instrument_close)

        # Assert
        assert exec_messages

    def test_process_order_book_depth_10(self) -> None:
        # Arrange - Create L2_MBP matching engine for depth10 data
        matching_engine_l2 = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L2_MBP,  # L2 for multi-level depth data
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            trade_execution=True,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )
        depth = TestDataStubs.order_book_depth10(instrument=self.instrument)
        assert matching_engine_l2.best_ask_price() is None
        assert matching_engine_l2.best_bid_price() is None

        # Act
        matching_engine_l2.process_order_book_depth10(depth)

        # Assert
        assert matching_engine_l2.best_ask_price() == depth.asks[0].price
        assert matching_engine_l2.best_bid_price() == depth.bids[0].price

    def test_process_trade_buyer_aggressor(self) -> None:
        # Arrange
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=1000.0,
            aggressor_side=AggressorSide.BUYER,
        )

        # Act
        self.matching_engine.process_trade_tick(trade)

        # Assert - Buyer aggressor should set ask price
        assert self.matching_engine.best_ask_price() == Price.from_str("1000.0")

    def test_process_trade_seller_aggressor(self) -> None:
        # Arrange
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=1000.0,
            aggressor_side=AggressorSide.SELLER,
        )

        # Act
        self.matching_engine.process_trade_tick(trade)

        # Assert - Seller aggressor should set bid price
        assert self.matching_engine.best_bid_price() == Price.from_str("1000.0")

    def test_process_trade_tick_no_aggressor_above_ask(self) -> None:
        # Arrange - Set initial bid/ask spread
        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=990.0,
            ask_price=1010.0,
        )
        self.matching_engine.process_quote_tick(quote)

        # Trade above ask with no aggressor
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=1020.0,
            aggressor_side=AggressorSide.NO_AGGRESSOR,
        )

        # Act
        self.matching_engine.process_trade_tick(trade)

        # Assert - L1_MBP book update_trade_tick sets both bid/ask to trade price
        # Then NO_AGGRESSOR logic doesn't modify further since 1020 >= 1020 (ask)
        assert self.matching_engine.best_ask_price() == Price.from_str("1020.0")
        assert self.matching_engine.best_bid_price() == Price.from_str("1020.0")

    def test_process_trade_tick_no_aggressor_within_spread(self) -> None:
        # Arrange - Set initial bid/ask spread
        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=990.0,
            ask_price=1010.0,
        )
        self.matching_engine.process_quote_tick(quote)

        # Trade within the spread with no aggressor
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=1000.0,
            aggressor_side=AggressorSide.NO_AGGRESSOR,
        )

        # Act
        self.matching_engine.process_trade_tick(trade)

        # Assert - L1_MBP book update_trade_tick sets both bid/ask to trade price
        assert self.matching_engine.best_bid_price() == Price.from_str("1000.0")
        assert self.matching_engine.best_ask_price() == Price.from_str("1000.0")

    def test_process_trade_tick_no_aggressor_below_bid(self) -> None:
        # Arrange - Set initial bid/ask spread
        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=1000.0,
            ask_price=1020.0,
        )
        self.matching_engine.process_quote_tick(quote)

        # Trade below current bid with no aggressor
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=990.0,
            aggressor_side=AggressorSide.NO_AGGRESSOR,
        )

        # Act
        self.matching_engine.process_trade_tick(trade)

        # Assert - L1_MBP book update_trade_tick sets both bid/ask to trade price
        assert self.matching_engine.best_bid_price() == Price.from_str("990.0")
        assert self.matching_engine.best_ask_price() == Price.from_str("990.0")

    def test_process_trade_tick_no_aggressor_at_bid_and_ask(self) -> None:
        # Arrange - Set initial bid/ask spread
        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=995.0,
            ask_price=1005.0,
        )
        self.matching_engine.process_quote_tick(quote)

        # Trade exactly at bid level with no aggressor
        trade1 = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=995.0,
            aggressor_side=AggressorSide.NO_AGGRESSOR,
        )

        # Act
        self.matching_engine.process_trade_tick(trade1)

        # Assert - L1_MBP book update_trade_tick sets both bid/ask to trade price
        assert self.matching_engine.best_bid_price() == Price.from_str("995.0")
        assert self.matching_engine.best_ask_price() == Price.from_str("995.0")

        # Trade exactly at ask level with no aggressor
        trade2 = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=1005.0,
            aggressor_side=AggressorSide.NO_AGGRESSOR,
        )

        # Act
        self.matching_engine.process_trade_tick(trade2)

        # Assert - L1_MBP book update_trade_tick sets both bid/ask to trade price
        assert self.matching_engine.best_bid_price() == Price.from_str("1005.0")
        assert self.matching_engine.best_ask_price() == Price.from_str("1005.0")

    def test_process_trade_tick_with_trade_execution_disabled(self) -> None:
        # Arrange - Create matching engine with trade_execution=False
        matching_engine = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L1_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            trade_execution=False,  # Disabled
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )

        # Process trade tick with BUYER aggressor
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=1000.0,
            aggressor_side=AggressorSide.BUYER,
        )

        # Act
        matching_engine.process_trade_tick(trade)

        # Assert - With trade_execution=False, only book update happens, no aggressor logic
        # L1_MBP book update_trade_tick sets both bid/ask to trade price
        assert matching_engine.best_bid_price() == Price.from_str("1000.0")
        assert matching_engine.best_ask_price() == Price.from_str("1000.0")

    def test_trade_execution_difference_buyer_aggressor(self) -> None:
        # This test demonstrates that trade_execution=True vs False produces the same result
        # for L1_MBP books since update_trade_tick sets both bid/ask to trade price anyway

        # Test with trade_execution=True (our main matching engine)
        trade_tick_enabled = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=1000.0,
            aggressor_side=AggressorSide.BUYER,
        )
        self.matching_engine.process_trade_tick(trade_tick_enabled)

        # Test with trade_execution=False
        matching_engine = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=1,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L1_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            trade_execution=False,  # Disabled
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )

        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=1000.0,
            aggressor_side=AggressorSide.BUYER,
        )
        matching_engine.process_trade_tick(trade)

        # Assert - Both should have same result for L1_MBP
        assert self.matching_engine.best_bid_price() == matching_engine.best_bid_price()
        assert self.matching_engine.best_ask_price() == matching_engine.best_ask_price()

    def test_fill_order_with_non_positive_qty_returns_early(self) -> None:
        # Arrange - Set initial bid/ask
        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=999.0,
            ask_price=1001.0,
        )
        self.matching_engine.process_quote_tick(quote)

        # Register to receive exec engine messages
        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Create a limit order that won't immediately fill
        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            price=Price.from_str("998.0"),  # Below ask so won't fill immediately
            quantity=self.instrument.make_qty(100.0),
        )

        # Process to register the order with the matching engine
        self.matching_engine.process_order(order, self.account_id)

        # Clear any initial order processing messages
        messages.clear()

        # Manually fill the order partially
        self.matching_engine.fill_order(
            order=order,
            last_px=Price.from_str("998.0"),
            last_qty=self.instrument.make_qty(50.0),
            liquidity_side=LiquiditySide.TAKER,
        )

        # Verify first fill was processed
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 1
        assert filled_events[0].last_qty == self.instrument.make_qty(50.0)

        # Clear messages
        messages.clear()

        # Act - Attempt to fill with quantity that would exceed remaining (should be clamped to 50)
        self.matching_engine.fill_order(
            order=order,
            last_px=Price.from_str("998.0"),
            last_qty=self.instrument.make_qty(60.0),  # 50 already filled, only 50 remains
            liquidity_side=LiquiditySide.TAKER,
        )

        # Should get a fill for remaining 50
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 1
        assert filled_events[0].last_qty == self.instrument.make_qty(50.0)

        # Clear messages
        messages.clear()

        # Act - Now try to fill again (should trigger early return due to non-positive qty)
        self.matching_engine.fill_order(
            order=order,
            last_px=Price.from_str("998.0"),
            last_qty=self.instrument.make_qty(10.0),
            liquidity_side=LiquiditySide.TAKER,
        )

        # Assert - No fill event should be emitted due to early return
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 0  # No fill should have been generated

    def test_buy_limit_fills_on_seller_trade_at_limit_price(self) -> None:
        # Regression test for GitHub issue where BUY LIMIT orders were not filling
        # when SELLER trades occurred at the limit price with trade_execution=True.
        #
        # Scenario:
        # 1. BUY LIMIT placed at 211.32 (timestamp 1762549820644390)
        # 2. SELLER trade occurs at 211.32 (timestamp 1762549826389000)
        # 3. Expected: Order should fill at the trade tick timestamp
        # 4. Actual (before fix): Order filled ~48µs later when OrderBookDelta moved ask

        # Set initial market state with bid/ask spread
        # (bid at 211.30, ask at 211.40 - order will rest in between)
        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=211.30,
            ask_price=211.40,
        )
        self.matching_engine.process_quote_tick(quote)

        # Register to capture order events
        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Place BUY LIMIT order at 211.32
        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            price=Price.from_str("211.32"),
            quantity=self.instrument.make_qty(1.0),
        )

        # Set clock to order placement time
        order_ts = 1762549820644390
        self.clock.set_time(order_ts)
        self.matching_engine.process_order(order, self.account_id)

        # Clear messages from order placement
        messages.clear()

        # Act - SELLER trade occurs at the limit price (someone hit the bid at 211.32)
        trade_ts = 1762549826389000
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=211.32,
            aggressor_side=AggressorSide.SELLER,
            ts_event=trade_ts,
            ts_init=trade_ts,
        )
        self.matching_engine.process_trade_tick(trade)

        # Assert - Order should be filled
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 1, (
            "BUY LIMIT should fill when SELLER trade occurs at limit price"
        )
        assert filled_events[0].order_side == OrderSide.BUY
        assert filled_events[0].last_px == Price.from_str("211.32")

    def test_sell_limit_fills_on_buyer_trade_at_limit_price(self) -> None:
        # Symmetric test: SELL LIMIT should fill when BUYER trade occurs at limit price

        # Set initial market state
        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=211.30,
            ask_price=211.40,
        )
        self.matching_engine.process_quote_tick(quote)

        # Register to capture order events
        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Place SELL LIMIT order at 211.38
        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=OrderSide.SELL,
            price=Price.from_str("211.38"),
            quantity=self.instrument.make_qty(1.0),
        )
        self.matching_engine.process_order(order, self.account_id)

        # Clear messages from order placement
        messages.clear()

        # Act - BUYER trade occurs at the limit price (someone took the ask at 211.38)
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=211.38,
            aggressor_side=AggressorSide.BUYER,
        )
        self.matching_engine.process_trade_tick(trade)

        # Assert - Order should be filled
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 1, (
            "SELL LIMIT should fill when BUYER trade occurs at limit price"
        )
        assert filled_events[0].order_side == OrderSide.SELL
        assert filled_events[0].last_px == Price.from_str("211.38")

    def test_trade_execution_does_not_persist_ghost_liquidity(self) -> None:
        # Verify that trade execution does not persist "ghost" liquidity.
        # Scenario:
        # 1. Market: Bid 100, Ask 110.
        # 2. Trade at 105 (Aggressor SELLER).
        #    - This transiently collapses Ask to 105 to check for fills.
        #    - But it should restore Ask to 110 immediately.
        # 3. New BUY LIMIT order at 106 arrives AFTER the trade.
        # 4. Expected: Should NOT fill (Ask should be back to 110).
        #    - If Ask persisted at 105 (Ghost), it would fill.

        # Arrange
        matching_engine = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L2_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            trade_execution=True,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )

        # Set initial market state (Wide spread)
        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=100.00,
            ask_price=110.00,
        )
        matching_engine.process_quote_tick(quote)

        # Register to capture order events
        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Act 1: Process Trade Tick at 105 (Seller Aggressor)
        trade_ts = 1762549826389000
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=105.00,
            aggressor_side=AggressorSide.SELLER,
            ts_event=trade_ts,
            ts_init=trade_ts,
        )
        self.clock.set_time(trade_ts)
        matching_engine.process_trade_tick(trade)

        # Act 2: Place BUY LIMIT order at 106.00 (Between Trade 105 and Old Ask 110)
        # If Ask is incorrectly 105, this will fill.
        # If Ask is correctly 110, this will NOT fill.
        order_ts = trade_ts + 1000  # 1ms later
        self.clock.set_time(order_ts)

        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            price=Price.from_str("106.00"),
            quantity=self.instrument.make_qty(1.0),
        )
        matching_engine.process_order(order, self.account_id)

        # Assert
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 0, (
            "BUY LIMIT filled against Ghost Ask! Ghost Liquidity persisted."
        )

    def test_trade_execution_fills_better_priced_orders_for_buys(self) -> None:
        # With transient override, a SELLER trade at P allows BUY orders at P or
        # better (higher limit) to fill - they would accept the trade price.
        #
        # Book: bid=211.30, ask=211.40
        # SELLER trade at 211.32 proves sell liquidity at 211.32
        # BUY LIMIT at 211.35 (willing to pay up to 211.35) should fill

        # Set initial market state
        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=211.30,
            ask_price=211.40,
        )
        self.matching_engine.process_quote_tick(quote)

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Place BUY LIMIT at 211.35 (willing to pay more than trade price)
        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            price=Price.from_str("211.35"),
            quantity=self.instrument.make_qty(1.0),
        )
        self.matching_engine.process_order(order, self.account_id)
        messages.clear()

        # SELLER trade at 211.32 - BUY at 211.35 should fill (willing to pay 211.35 >= 211.32)
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=211.32,
            aggressor_side=AggressorSide.SELLER,
        )
        self.matching_engine.process_trade_tick(trade)

        # Assert - Order should fill (211.35 >= trade price 211.32)
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 1, "BUY LIMIT at 211.35 should fill on SELLER trade at 211.32"

    def test_trade_execution_fills_better_priced_orders_for_sells(self) -> None:
        # With transient override, a BUYER trade at P allows SELL orders at P or
        # better (lower limit) to fill - they would accept the trade price.
        #
        # Book: bid=211.30, ask=211.40
        # BUYER trade at 211.38 proves buy liquidity at 211.38
        # SELL LIMIT at 211.35 (willing to sell down to 211.35) should fill

        # Set initial market state
        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=211.30,
            ask_price=211.40,
        )
        self.matching_engine.process_quote_tick(quote)

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Place SELL LIMIT at 211.35 (willing to sell lower than trade price)
        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=OrderSide.SELL,
            price=Price.from_str("211.35"),
            quantity=self.instrument.make_qty(1.0),
        )
        self.matching_engine.process_order(order, self.account_id)
        messages.clear()

        # BUYER trade at 211.38 - SELL at 211.35 should fill (willing to sell 211.35 <= 211.38)
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=211.38,
            aggressor_side=AggressorSide.BUYER,
        )
        self.matching_engine.process_trade_tick(trade)

        # Assert - Order should fill (211.35 <= trade price 211.38)
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 1, "SELL LIMIT at 211.35 should fill on BUYER trade at 211.38"

    def test_trade_execution_restores_matching_state_after_seller_trade(self) -> None:
        # Verify the transient override restores matching state after processing.
        # If not restored, new orders placed after the trade would fill immediately.

        # Set initial market state
        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=211.30,
            ask_price=211.40,
        )
        self.matching_engine.process_quote_tick(quote)

        # Process SELLER trade at 211.32 (transiently sets ask=211.32, then restores)
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=211.32,
            aggressor_side=AggressorSide.SELLER,
        )
        self.matching_engine.process_trade_tick(trade)

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Place BUY LIMIT at 211.35 AFTER the trade
        # If ask wasn't restored (stuck at 211.32), this would fill immediately
        # If ask was restored (back to 211.40), this should NOT fill
        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            price=Price.from_str("211.35"),
            quantity=self.instrument.make_qty(1.0),
        )
        self.matching_engine.process_order(order, self.account_id)

        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 0, (
            "BUY LIMIT at 211.35 should NOT fill immediately - matching state was restored"
        )

    def test_trade_execution_restores_matching_state_after_buyer_trade(self) -> None:
        # Verify the transient override restores matching state after processing.

        # Set initial market state
        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=211.30,
            ask_price=211.40,
        )
        self.matching_engine.process_quote_tick(quote)

        # Process BUYER trade at 211.38 (transiently sets bid=211.38, then restores)
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=211.38,
            aggressor_side=AggressorSide.BUYER,
        )
        self.matching_engine.process_trade_tick(trade)

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Place SELL LIMIT at 211.35 AFTER the trade
        # If bid wasn't restored (stuck at 211.38), this would fill immediately
        # If bid was restored (back to 211.30), this should NOT fill
        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=OrderSide.SELL,
            price=Price.from_str("211.35"),
            quantity=self.instrument.make_qty(1.0),
        )
        self.matching_engine.process_order(order, self.account_id)

        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 0, (
            "SELL LIMIT at 211.35 should NOT fill immediately - matching state was restored"
        )

    def test_trade_execution_same_side_sell_does_not_fill_on_seller_trade(self) -> None:
        # SELL orders should not fill on SELLER trades (same side).
        # Only opposite-side orders match against the trade.

        # Set initial market state
        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=211.30,
            ask_price=211.40,
        )
        self.matching_engine.process_quote_tick(quote)

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Place SELL LIMIT at 211.35 (above trade price)
        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=OrderSide.SELL,
            price=Price.from_str("211.35"),
            quantity=self.instrument.make_qty(1.0),
        )
        self.matching_engine.process_order(order, self.account_id)
        messages.clear()

        # SELLER trade at 211.32 - same side, should NOT trigger SELL fills
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=211.32,
            aggressor_side=AggressorSide.SELLER,
        )
        self.matching_engine.process_trade_tick(trade)

        # Assert - SELL order should NOT fill on SELLER trade
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 0, "SELL LIMIT should NOT fill on SELLER trade (same side)"

    def test_trade_execution_same_side_buy_does_not_fill_on_buyer_trade(self) -> None:
        # BUY orders should not fill on BUYER trades (same side).

        # Set initial market state
        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=211.30,
            ask_price=211.40,
        )
        self.matching_engine.process_quote_tick(quote)

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Place BUY LIMIT at 211.35 (below trade price)
        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            price=Price.from_str("211.35"),
            quantity=self.instrument.make_qty(1.0),
        )
        self.matching_engine.process_order(order, self.account_id)
        messages.clear()

        # BUYER trade at 211.38 - same side, should NOT trigger BUY fills
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=211.38,
            aggressor_side=AggressorSide.BUYER,
        )
        self.matching_engine.process_trade_tick(trade)

        # Assert - BUY order should NOT fill on BUYER trade
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 0, "BUY LIMIT should NOT fill on BUYER trade (same side)"

    def test_trade_execution_with_l2_mbp_order_book_deltas(self) -> None:
        # 1. BUY LIMIT at 211.32 placed
        # 2. Order book established via deltas (bid=211.30, ask=211.40)
        # 3. SELLER trade at 211.32 proves liquidity
        # 4. Expected: Order fills at trade tick timestamp

        # Arrange - Create L2_MBP matching engine
        matching_engine_l2 = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L2_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            trade_execution=True,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )

        # Set initial market state via OrderBookDeltas (L2 style)
        snapshot = TestDataStubs.order_book_snapshot(
            instrument=self.instrument,
            bid_price=211.30,
            ask_price=211.40,
            bid_size=100.0,
            ask_size=100.0,
            bid_levels=1,
            ask_levels=1,
        )
        matching_engine_l2.process_order_book_deltas(snapshot)

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Place BUY LIMIT at 211.32 (inside the spread)
        order_ts = 1762549820644390
        self.clock.set_time(order_ts)
        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            price=Price.from_str("211.32"),
            quantity=self.instrument.make_qty(1.0),
        )
        matching_engine_l2.process_order(order, self.account_id)
        messages.clear()

        # SELLER trade at 211.32 - should trigger fill immediately
        trade_ts = 1762549826389000
        self.clock.set_time(trade_ts)
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=211.32,
            aggressor_side=AggressorSide.SELLER,
            ts_event=trade_ts,
            ts_init=trade_ts,
        )
        matching_engine_l2.process_trade_tick(trade)

        # Assert - Order should fill at trade tick timestamp
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 1, (
            "BUY LIMIT at 211.32 should fill on SELLER trade at 211.32 with L2_MBP"
        )
        assert filled_events[0].ts_event == trade_ts, (
            f"Fill should occur at trade timestamp {trade_ts}, got {filled_events[0].ts_event}"
        )

    def test_trade_execution_complete_fill_when_trade_exceeds_order(self) -> None:
        # Test that when trade size exceeds remaining order quantity,
        # the fill is capped at the remaining order quantity.
        #
        # Scenario:
        # 1. BUY LIMIT placed with qty=50
        # 2. SELLER trade with qty=100 (larger than order)
        # 3. Expected: Order fully filled for 50 (not 100)

        # Arrange - Create L2_MBP matching engine
        matching_engine_l2 = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L2_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            trade_execution=True,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )

        # Set initial market state
        snapshot = TestDataStubs.order_book_snapshot(
            instrument=self.instrument,
            bid_price=1000.00,
            ask_price=1010.00,
            bid_size=100.0,
            ask_size=100.0,
            bid_levels=1,
            ask_levels=1,
        )
        matching_engine_l2.process_order_book_deltas(snapshot)

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Place small BUY LIMIT order (50 qty)
        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            price=Price.from_str("1005.00"),
            quantity=self.instrument.make_qty(50.0),
        )
        matching_engine_l2.process_order(order, self.account_id)
        messages.clear()

        # Act - Large trade (100 qty, more than order size)
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=1005.00,
            size=100.0,
            aggressor_side=AggressorSide.SELLER,
        )
        matching_engine_l2.process_trade_tick(trade)

        # Assert - Order should be filled for 50 (min of order size and trade size)
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 1
        assert filled_events[0].last_qty == self.instrument.make_qty(50.0), (
            f"Fill qty should be capped at order size 50, got {filled_events[0].last_qty}"
        )

    def test_modify_partially_filled_order_quantity_below_filled_rejected(self) -> None:
        # Tests that modifying a partially filled order to a quantity below filled_qty is rejected
        # Arrange - Create L2_MBP matching engine
        matching_engine_l2 = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L2_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            trade_execution=True,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )

        # Set initial market state with partial liquidity
        snapshot = TestDataStubs.order_book_snapshot(
            instrument=self.instrument,
            bid_price=1490.00,
            ask_price=1500.00,
            bid_size=100.0,
            ask_size=50.0,  # Only 50 available at ask
            bid_levels=1,
            ask_levels=1,
        )
        matching_engine_l2.process_order_book_deltas(snapshot)

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Place BUY LIMIT order at ask (will match and partially fill)
        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            price=Price.from_str("1500.00"),
            quantity=self.instrument.make_qty(100.0),
        )
        matching_engine_l2.process_order(order, self.account_id)

        # Order should be partially filled (50 of 100)
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 1
        assert filled_events[0].last_qty == self.instrument.make_qty(50.0)
        messages.clear()

        # Act - Attempt to modify quantity to 40, below filled_qty of 50
        modify_command = ModifyOrder(
            trader_id=self.trader_id,
            strategy_id=StrategyId("S-001"),
            instrument_id=self.instrument.id,
            client_order_id=order.client_order_id,
            venue_order_id=VenueOrderId("V-001"),
            quantity=Quantity.from_str("40.000"),
            price=None,
            trigger_price=None,
            command_id=UUID4(),
            ts_init=0,
        )
        matching_engine_l2.process_modify(modify_command, self.account_id)

        # Assert - Should receive OrderModifyRejected
        rejected_events = [m for m in messages if isinstance(m, OrderModifyRejected)]
        assert len(rejected_events) == 1, (
            f"Expected OrderModifyRejected, got {[type(m).__name__ for m in messages]}"
        )
        assert "below filled quantity" in rejected_events[0].reason

    @pytest.mark.parametrize(
        ("order_side", "book_side", "order_price", "opposite_price"),
        [
            (OrderSide.BUY, OrderSide.SELL, "100.00", "90.00"),
            (OrderSide.SELL, OrderSide.BUY, "100.00", "110.00"),
        ],
        ids=["buy_limit", "sell_limit"],
    )
    def test_partial_fill_uses_current_book_liquidity(
        self,
        order_side: OrderSide,
        book_side: OrderSide,
        order_price: str,
        opposite_price: str,
    ) -> None:
        # Test: partial fills should use current book liquidity.
        #
        # Scenario:
        # 1. LIMIT order at price with qty=200
        # 2. First delta adds 10 @ price → fills 10
        # 3. Second delta UPDATES to 50 @ price → fills 50 (current book liquidity)

        # Arrange
        matching_engine_l2 = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L2_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            trade_execution=False,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Initialize matching core with opposite side
        opposite_delta = OrderBookDelta(
            instrument_id=self.instrument.id,
            action=BookAction.ADD,
            order=BookOrder(
                side=order_side,
                price=Price.from_str(opposite_price),
                size=Quantity.from_str("100.000"),
                order_id=100,
            ),
            flags=0,
            sequence=0,
            ts_event=0,
            ts_init=0,
        )
        matching_engine_l2.process_order_book_delta(opposite_delta)

        delta_1 = OrderBookDelta(
            instrument_id=self.instrument.id,
            action=BookAction.ADD,
            order=BookOrder(
                side=book_side,
                price=Price.from_str(order_price),
                size=Quantity.from_str("10.000"),
                order_id=1,
            ),
            flags=0,
            sequence=1,
            ts_event=0,
            ts_init=0,
        )
        matching_engine_l2.process_order_book_delta(delta_1)

        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=order_side,
            price=Price.from_str(order_price),
            quantity=self.instrument.make_qty(200.0),
        )

        # Act
        matching_engine_l2.process_order(order, self.account_id)

        # Assert
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 1, f"Expected 1 fill, got {len(filled_events)}"
        assert filled_events[0].last_qty == self.instrument.make_qty(10.0), (
            f"First fill should be 10, got {filled_events[0].last_qty}"
        )
        messages.clear()

        # Act - Update same price level to 50 units
        delta_2 = OrderBookDelta(
            instrument_id=self.instrument.id,
            action=BookAction.UPDATE,
            order=BookOrder(
                side=book_side,
                price=Price.from_str(order_price),
                size=Quantity.from_str("50.000"),
                order_id=1,
            ),
            flags=0,
            sequence=2,
            ts_event=1,
            ts_init=1,
        )
        matching_engine_l2.process_order_book_delta(delta_2)

        # Assert - fills against current book liquidity (50)
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 1, (
            f"Expected 1 fill event after second delta, got {len(filled_events)}"
        )
        assert filled_events[0].last_qty == self.instrument.make_qty(50.0), (
            f"Second fill should be 50 (current book liquidity), "
            f"got {filled_events[0].last_qty}"
        )

    def test_new_liquidity_at_better_price_fills(self) -> None:
        # Test: new liquidity at better prices should fill.
        #
        # Scenario:
        # 1. BUY LIMIT at 100, size=200
        # 2. SELL Delta at 100, size=10 → fills 10
        # 3. SELL Delta at 90, size=50 → fills against all current book liquidity

        # Arrange
        matching_engine_l2 = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L2_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            trade_execution=False,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Initialize matching core with bid side
        bid_delta = OrderBookDelta(
            instrument_id=self.instrument.id,
            action=BookAction.ADD,
            order=BookOrder(
                side=OrderSide.BUY,
                price=Price.from_str("80.00"),
                size=Quantity.from_str("100.000"),
                order_id=100,
            ),
            flags=0,
            sequence=0,
            ts_event=0,
            ts_init=0,
        )
        matching_engine_l2.process_order_book_delta(bid_delta)

        ask_delta_1 = OrderBookDelta(
            instrument_id=self.instrument.id,
            action=BookAction.ADD,
            order=BookOrder(
                side=OrderSide.SELL,
                price=Price.from_str("100.00"),
                size=Quantity.from_str("10.000"),
                order_id=1,
            ),
            flags=0,
            sequence=1,
            ts_event=0,
            ts_init=0,
        )
        matching_engine_l2.process_order_book_delta(ask_delta_1)

        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            price=Price.from_str("100.00"),
            quantity=self.instrument.make_qty(200.0),
        )

        # Act
        matching_engine_l2.process_order(order, self.account_id)

        # Assert
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 1, f"Expected 1 fill, got {len(filled_events)}"
        assert filled_events[0].last_qty == self.instrument.make_qty(10.0)
        messages.clear()

        # Act - New liquidity at better price (90 vs limit 100)
        ask_delta_2 = OrderBookDelta(
            instrument_id=self.instrument.id,
            action=BookAction.ADD,
            order=BookOrder(
                side=OrderSide.SELL,
                price=Price.from_str("90.00"),
                size=Quantity.from_str("50.000"),
                order_id=2,
            ),
            flags=0,
            sequence=2,
            ts_event=1,
            ts_init=1,
        )
        matching_engine_l2.process_order_book_delta(ask_delta_2)

        # Assert - Fills against current book liquidity (50 @ 90 + 10 @ 100 = 60)
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        total_filled = sum(e.last_qty.as_double() for e in filled_events)
        assert total_filled == 60.0, (
            f"Total fill should be 60 (current book liquidity). "
            f"Got {total_filled} from {len(filled_events)} fill(s)"
        )

    def test_fully_filled_order_not_rematched_on_subsequent_iterate(self) -> None:
        # A fully-filled order should be removed from matching core to prevent
        # duplicate fills on subsequent iterate() calls.

        # Arrange
        matching_engine_l2 = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L2_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            trade_execution=False,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        bid_delta = OrderBookDelta(
            instrument_id=self.instrument.id,
            action=BookAction.ADD,
            order=BookOrder(
                side=OrderSide.BUY,
                price=Price.from_str("90.00"),
                size=Quantity.from_str("100.000"),
                order_id=100,
            ),
            flags=0,
            sequence=0,
            ts_event=0,
            ts_init=0,
        )
        matching_engine_l2.process_order_book_delta(bid_delta)

        ask_delta = OrderBookDelta(
            instrument_id=self.instrument.id,
            action=BookAction.ADD,
            order=BookOrder(
                side=OrderSide.SELL,
                price=Price.from_str("100.00"),
                size=Quantity.from_str("50.000"),
                order_id=1,
            ),
            flags=0,
            sequence=1,
            ts_event=0,
            ts_init=0,
        )
        matching_engine_l2.process_order_book_delta(ask_delta)

        order = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            price=Price.from_str("100.00"),
            quantity=self.instrument.make_qty(50.0),
        )
        matching_engine_l2.process_order(order, self.account_id)

        # Act
        matching_engine_l2.iterate(timestamp_ns=1)

        # Assert
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 1, (
            f"Expected exactly 1 fill (initial), but got {len(filled_events)} "
            f"(duplicate fill on subsequent iterate)"
        )

    def test_liquidity_consumption_tracks_fills_at_price_level(self):
        """
        Test that with liquidity_consumption=True, fills consume available liquidity at
        each price level, reducing subsequent fill quantities.
        """
        matching_engine = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L2_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            liquidity_consumption=True,
        )

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Establish market with bid side
        bid_delta = OrderBookDelta(
            instrument_id=self.instrument.id,
            action=BookAction.ADD,
            order=BookOrder(
                side=OrderSide.BUY,
                price=Price.from_str("90.00"),
                size=Quantity.from_str("100.000"),
                order_id=100,
            ),
            flags=0,
            sequence=0,
            ts_event=0,
            ts_init=0,
        )
        matching_engine.process_order_book_delta(bid_delta)

        # Add 100 units of liquidity at ask 100.00
        ask_delta = OrderBookDelta(
            instrument_id=self.instrument.id,
            action=BookAction.ADD,
            order=BookOrder(
                side=OrderSide.SELL,
                price=Price.from_str("100.00"),
                size=Quantity.from_str("100.000"),
                order_id=1,
            ),
            flags=0,
            sequence=1,
            ts_event=0,
            ts_init=0,
        )
        matching_engine.process_order_book_delta(ask_delta)

        # First order: BUY 30 units - should fill 30
        order1 = TestExecStubs.market_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(30.0),
            client_order_id=TestIdStubs.client_order_id(1),
        )
        matching_engine.process_order(order1, self.account_id)
        matching_engine.iterate(timestamp_ns=1)

        # Second order: BUY 50 units - should fill 50 (80 consumed, 20 remaining)
        order2 = TestExecStubs.market_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(50.0),
            client_order_id=TestIdStubs.client_order_id(2),
        )
        matching_engine.process_order(order2, self.account_id)
        matching_engine.iterate(timestamp_ns=2)

        # Third order: BUY 50 units - should only fill 20 (remaining liquidity)
        order3 = TestExecStubs.market_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(50.0),
            client_order_id=TestIdStubs.client_order_id(3),
        )
        matching_engine.process_order(order3, self.account_id)
        matching_engine.iterate(timestamp_ns=3)

        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 3

        # First fill: 30 units
        assert filled_events[0].last_qty == Quantity.from_str("30.000")

        # Second fill: 50 units
        assert filled_events[1].last_qty == Quantity.from_str("50.000")

        # Third fill: only 20 units (100 - 30 - 50 = 20 remaining)
        assert filled_events[2].last_qty == Quantity.from_str("20.000")

    def test_liquidity_consumption_resets_on_fresh_data(self):
        """
        Test that consumption resets when fresh data arrives at a price level.
        """
        matching_engine = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L2_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            liquidity_consumption=True,
        )

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Establish market with bid side
        bid_delta = OrderBookDelta(
            instrument_id=self.instrument.id,
            action=BookAction.ADD,
            order=BookOrder(
                side=OrderSide.BUY,
                price=Price.from_str("90.00"),
                size=Quantity.from_str("100.000"),
                order_id=100,
            ),
            flags=0,
            sequence=0,
            ts_event=0,
            ts_init=0,
        )
        matching_engine.process_order_book_delta(bid_delta)

        # Add 50 units of liquidity at ask 100.00
        ask_delta = OrderBookDelta(
            instrument_id=self.instrument.id,
            action=BookAction.ADD,
            order=BookOrder(
                side=OrderSide.SELL,
                price=Price.from_str("100.00"),
                size=Quantity.from_str("50.000"),
                order_id=1,
            ),
            flags=0,
            sequence=1,
            ts_event=0,
            ts_init=0,
        )
        matching_engine.process_order_book_delta(ask_delta)

        # First order: BUY 50 units - consumes all liquidity
        order1 = TestExecStubs.market_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(50.0),
            client_order_id=TestIdStubs.client_order_id(1),
        )
        matching_engine.process_order(order1, self.account_id)
        matching_engine.iterate(timestamp_ns=1)

        # Fresh data: Update the level to 80 units (simulates new liquidity)
        ask_update = OrderBookDelta(
            instrument_id=self.instrument.id,
            action=BookAction.UPDATE,
            order=BookOrder(
                side=OrderSide.SELL,
                price=Price.from_str("100.00"),
                size=Quantity.from_str("80.000"),
                order_id=1,
            ),
            flags=0,
            sequence=2,
            ts_event=1,
            ts_init=1,
        )
        matching_engine.process_order_book_delta(ask_update)

        # Second order: BUY 60 units - should fill 60 (fresh 80 available)
        order2 = TestExecStubs.market_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(60.0),
            client_order_id=TestIdStubs.client_order_id(2),
        )
        matching_engine.process_order(order2, self.account_id)
        matching_engine.iterate(timestamp_ns=2)

        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 2
        assert filled_events[0].last_qty == Quantity.from_str("50.000")
        assert filled_events[1].last_qty == Quantity.from_str("60.000")

    def test_liquidity_consumption_off_allows_repeated_fills(self):
        """
        Test that with liquidity_consumption=False, the same liquidity can be consumed
        by multiple orders (default behavior).
        """
        matching_engine = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L2_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            liquidity_consumption=False,  # Explicitly off
        )

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        # Establish market with bid side
        bid_delta = OrderBookDelta(
            instrument_id=self.instrument.id,
            action=BookAction.ADD,
            order=BookOrder(
                side=OrderSide.BUY,
                price=Price.from_str("90.00"),
                size=Quantity.from_str("100.000"),
                order_id=100,
            ),
            flags=0,
            sequence=0,
            ts_event=0,
            ts_init=0,
        )
        matching_engine.process_order_book_delta(bid_delta)

        # Add 50 units of liquidity at ask 100.00
        ask_delta = OrderBookDelta(
            instrument_id=self.instrument.id,
            action=BookAction.ADD,
            order=BookOrder(
                side=OrderSide.SELL,
                price=Price.from_str("100.00"),
                size=Quantity.from_str("50.000"),
                order_id=1,
            ),
            flags=0,
            sequence=1,
            ts_event=0,
            ts_init=0,
        )
        matching_engine.process_order_book_delta(ask_delta)

        # First order: BUY 50 units
        order1 = TestExecStubs.market_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(50.0),
            client_order_id=TestIdStubs.client_order_id(1),
        )
        matching_engine.process_order(order1, self.account_id)
        matching_engine.iterate(timestamp_ns=1)

        # Second order: BUY 50 units - should also fill 50 (no consumption tracking)
        order2 = TestExecStubs.market_order(
            instrument=self.instrument,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(50.0),
            client_order_id=TestIdStubs.client_order_id(2),
        )
        matching_engine.process_order(order2, self.account_id)
        matching_engine.iterate(timestamp_ns=2)

        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 2
        assert filled_events[0].last_qty == Quantity.from_str("50.000")
        assert filled_events[1].last_qty == Quantity.from_str("50.000")

    @pytest.mark.parametrize(
        ("order_side", "aggressor_side"),
        [
            (OrderSide.BUY, AggressorSide.SELLER),
            (OrderSide.SELL, AggressorSide.BUYER),
        ],
    )
    def test_trade_consumption_prevents_overfill(
        self,
        order_side: OrderSide,
        aggressor_side: AggressorSide,
    ) -> None:
        """
        Test that with trade_execution=True and liquidity_consumption=True, multiple
        orders matching a single trade tick have their total fills capped to the trade
        size.

        Covers both BUY orders (filled by SELLER trades) and SELL orders (filled by
        BUYER trades).

        """
        matching_engine = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L1_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            trade_execution=True,
            liquidity_consumption=True,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=90.00,
            ask_price=110.00,
        )
        matching_engine.process_quote_tick(quote)

        order1 = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=order_side,
            price=Price.from_str("100.00"),
            quantity=self.instrument.make_qty(30.0),
            client_order_id=TestIdStubs.client_order_id(1),
        )
        matching_engine.process_order(order1, self.account_id)

        order2 = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=order_side,
            price=Price.from_str("100.00"),
            quantity=self.instrument.make_qty(30.0),
            client_order_id=TestIdStubs.client_order_id(2),
        )
        matching_engine.process_order(order2, self.account_id)

        messages.clear()

        # Trade with 50 units, less than orders' 60 total
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=100.00,
            size=50.0,
            aggressor_side=aggressor_side,
        )
        matching_engine.process_trade_tick(trade)

        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 2
        assert filled_events[0].last_qty == Quantity.from_str("30.000")
        assert filled_events[1].last_qty == Quantity.from_str("20.000")

    @pytest.mark.parametrize(
        ("order_side", "aggressor_side"),
        [
            (OrderSide.BUY, AggressorSide.SELLER),
            (OrderSide.SELL, AggressorSide.BUYER),
        ],
    )
    def test_trade_consumption_disabled_allows_overfill(
        self,
        order_side: OrderSide,
        aggressor_side: AggressorSide,
    ) -> None:
        """
        Test that with trade_execution=True but liquidity_consumption=False, the legacy
        overfill behavior is preserved (each order can fill up to the full trade size
        independently).

        Covers both BUY and SELL orders.

        """
        matching_engine = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L1_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            trade_execution=True,
            liquidity_consumption=False,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=90.00,
            ask_price=110.00,
        )
        matching_engine.process_quote_tick(quote)

        order1 = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=order_side,
            price=Price.from_str("100.00"),
            quantity=self.instrument.make_qty(30.0),
            client_order_id=TestIdStubs.client_order_id(1),
        )
        matching_engine.process_order(order1, self.account_id)

        order2 = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=order_side,
            price=Price.from_str("100.00"),
            quantity=self.instrument.make_qty(30.0),
            client_order_id=TestIdStubs.client_order_id(2),
        )
        matching_engine.process_order(order2, self.account_id)

        messages.clear()

        # Trade with 50 units, less than orders' 60 total
        trade = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=100.00,
            size=50.0,
            aggressor_side=aggressor_side,
        )
        matching_engine.process_trade_tick(trade)

        # Both fill fully (60 total overfills 50 trade) - legacy behavior
        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 2
        assert filled_events[0].last_qty == Quantity.from_str("30.000")
        assert filled_events[1].last_qty == Quantity.from_str("30.000")

    @pytest.mark.parametrize(
        ("order_side", "aggressor_side"),
        [
            (OrderSide.BUY, AggressorSide.SELLER),
            (OrderSide.SELL, AggressorSide.BUYER),
        ],
    )
    def test_trade_consumption_resets_on_fresh_trade(
        self,
        order_side: OrderSide,
        aggressor_side: AggressorSide,
    ) -> None:
        """
        Test that trade consumption resets when a fresh trade tick arrives, allowing new
        fills against the fresh liquidity.
        """
        matching_engine = OrderMatchingEngine(
            instrument=self.instrument,
            raw_id=0,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            book_type=BookType.L1_MBP,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            reject_stop_orders=True,
            trade_execution=True,
            liquidity_consumption=True,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )

        messages: list[Any] = []
        self.msgbus.register("ExecEngine.process", messages.append)

        quote = TestDataStubs.quote_tick(
            instrument=self.instrument,
            bid_price=90.00,
            ask_price=110.00,
        )
        matching_engine.process_quote_tick(quote)

        order1 = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=order_side,
            price=Price.from_str("100.00"),
            quantity=self.instrument.make_qty(30.0),
            client_order_id=TestIdStubs.client_order_id(1),
        )
        matching_engine.process_order(order1, self.account_id)

        order2 = TestExecStubs.limit_order(
            instrument=self.instrument,
            order_side=order_side,
            price=Price.from_str("100.00"),
            quantity=self.instrument.make_qty(30.0),
            client_order_id=TestIdStubs.client_order_id(2),
        )
        matching_engine.process_order(order2, self.account_id)

        messages.clear()

        trade1 = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=100.00,
            size=40.0,
            aggressor_side=aggressor_side,
        )
        matching_engine.process_trade_tick(trade1)

        # Fresh trade resets consumption
        trade2 = TestDataStubs.trade_tick(
            instrument=self.instrument,
            price=100.00,
            size=50.0,
            aggressor_side=aggressor_side,
        )
        matching_engine.process_trade_tick(trade2)

        filled_events = [m for m in messages if isinstance(m, OrderFilled)]
        assert len(filled_events) == 3
        assert filled_events[0].last_qty == Quantity.from_str("30.000")
        assert filled_events[1].last_qty == Quantity.from_str("10.000")
        assert filled_events[2].last_qty == Quantity.from_str("20.000")
