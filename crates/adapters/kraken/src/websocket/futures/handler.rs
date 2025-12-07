// -------------------------------------------------------------------------------------------------
//  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
//  https://nautechsystems.io
//
//  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
//  You may not use this file except in compliance with the License.
//  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
// -------------------------------------------------------------------------------------------------

//! WebSocket message handler for Kraken Futures.

use std::{
    collections::VecDeque,
    fmt::Debug,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};

use ahash::AHashMap;
use dashmap::DashSet;
use nautilus_common::cache::quote::QuoteCache;
use nautilus_core::{AtomicTime, UUID4, UnixNanos, time::get_atomic_clock_realtime};
use nautilus_model::{
    data::{
        BookOrder, IndexPriceUpdate, MarkPriceUpdate, OrderBookDelta, OrderBookDeltas, TradeTick,
    },
    enums::{
        AggressorSide, BookAction, LiquiditySide, OrderSide, OrderStatus, OrderType, TimeInForce,
    },
    identifiers::{AccountId, ClientOrderId, InstrumentId, Symbol, TradeId, VenueOrderId},
    instruments::{Instrument, InstrumentAny},
    reports::{FillReport, OrderStatusReport},
    types::{Money, Price, Quantity},
};
use nautilus_network::websocket::WebSocketClient;
use serde::Deserialize;
use tokio_tungstenite::tungstenite::Message;
use ustr::Ustr;

use super::messages::{
    KrakenFuturesBookDelta, KrakenFuturesBookSnapshot, KrakenFuturesChallengeRequest,
    KrakenFuturesEvent, KrakenFuturesFeed, KrakenFuturesFill, KrakenFuturesFillsDelta,
    KrakenFuturesFillsSnapshot, KrakenFuturesOpenOrder, KrakenFuturesOpenOrdersDelta,
    KrakenFuturesOpenOrdersSnapshot, KrakenFuturesPrivateSubscribeRequest, KrakenFuturesTickerData,
    KrakenFuturesTradeData, KrakenFuturesTradeSnapshot, KrakenFuturesWsMessage,
};
use crate::common::enums::KrakenOrderSide;

/// Commands sent from the outer client to the inner message handler.
#[allow(
    clippy::large_enum_variant,
    reason = "Commands are ephemeral and immediately consumed"
)]
pub enum HandlerCommand {
    SetClient(WebSocketClient),
    SubscribeTicker(Symbol),
    UnsubscribeTicker(Symbol),
    SubscribeTrade(Symbol),
    UnsubscribeTrade(Symbol),
    SubscribeBook(Symbol),
    UnsubscribeBook(Symbol),
    Disconnect,
    InitializeInstruments(Vec<InstrumentAny>),
    UpdateInstrument(InstrumentAny),
    SetAccountId(AccountId),
    RequestChallenge {
        api_key: String,
        response_tx: tokio::sync::oneshot::Sender<String>,
    },
    SetAuthCredentials {
        api_key: String,
        original_challenge: String,
        signed_challenge: String,
    },
    SubscribeOpenOrders,
    SubscribeFills,
    CacheClientOrder(ClientOrderId, InstrumentId),
}

impl Debug for HandlerCommand {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::SetClient(_) => f.debug_struct("SetClient").finish(),
            Self::SubscribeTicker(s) => f.debug_tuple("SubscribeTicker").field(s).finish(),
            Self::UnsubscribeTicker(s) => f.debug_tuple("UnsubscribeTicker").field(s).finish(),
            Self::SubscribeTrade(s) => f.debug_tuple("SubscribeTrade").field(s).finish(),
            Self::UnsubscribeTrade(s) => f.debug_tuple("UnsubscribeTrade").field(s).finish(),
            Self::SubscribeBook(s) => f.debug_tuple("SubscribeBook").field(s).finish(),
            Self::UnsubscribeBook(s) => f.debug_tuple("UnsubscribeBook").field(s).finish(),
            Self::Disconnect => write!(f, "Disconnect"),
            Self::InitializeInstruments(v) => f
                .debug_tuple("InitializeInstruments")
                .field(&v.len())
                .finish(),
            Self::UpdateInstrument(i) => f.debug_tuple("UpdateInstrument").field(&i.id()).finish(),
            Self::SetAccountId(id) => f.debug_tuple("SetAccountId").field(id).finish(),
            Self::RequestChallenge { api_key, .. } => {
                let masked = &api_key[..4.min(api_key.len())];
                f.debug_struct("RequestChallenge")
                    .field("api_key", &format!("{masked}..."))
                    .finish()
            }
            Self::SetAuthCredentials { api_key, .. } => {
                let masked = &api_key[..4.min(api_key.len())];
                f.debug_struct("SetAuthCredentials")
                    .field("api_key", &format!("{masked}..."))
                    .finish()
            }
            Self::SubscribeOpenOrders => write!(f, "SubscribeOpenOrders"),
            Self::SubscribeFills => write!(f, "SubscribeFills"),
            Self::CacheClientOrder(c, i) => {
                f.debug_tuple("CacheClientOrder").field(c).field(i).finish()
            }
        }
    }
}

/// WebSocket message handler for Kraken Futures.
pub struct FuturesFeedHandler {
    clock: &'static AtomicTime,
    signal: Arc<AtomicBool>,
    client: Option<WebSocketClient>,
    cmd_rx: tokio::sync::mpsc::UnboundedReceiver<HandlerCommand>,
    raw_rx: tokio::sync::mpsc::UnboundedReceiver<Message>,
    subscriptions: Arc<DashSet<String>>,
    instruments_cache: AHashMap<Ustr, InstrumentAny>,
    quote_cache: QuoteCache,
    pending_messages: VecDeque<KrakenFuturesWsMessage>,
    account_id: Option<AccountId>,
    api_key: Option<String>,
    original_challenge: Option<String>,
    signed_challenge: Option<String>,
    client_order_instruments: AHashMap<String, InstrumentId>,
    pending_challenge_tx: Option<tokio::sync::oneshot::Sender<String>>,
}

impl FuturesFeedHandler {
    /// Creates a new [`FuturesFeedHandler`] instance.
    pub fn new(
        signal: Arc<AtomicBool>,
        cmd_rx: tokio::sync::mpsc::UnboundedReceiver<HandlerCommand>,
        raw_rx: tokio::sync::mpsc::UnboundedReceiver<Message>,
        subscriptions: Arc<DashSet<String>>,
    ) -> Self {
        Self {
            clock: get_atomic_clock_realtime(),
            signal,
            client: None,
            cmd_rx,
            raw_rx,
            subscriptions,
            instruments_cache: AHashMap::new(),
            quote_cache: QuoteCache::new(),
            pending_messages: VecDeque::new(),
            account_id: None,
            api_key: None,
            original_challenge: None,
            signed_challenge: None,
            client_order_instruments: AHashMap::new(),
            pending_challenge_tx: None,
        }
    }

    pub fn is_stopped(&self) -> bool {
        self.signal.load(Ordering::Relaxed)
    }

    fn get_instrument(&self, symbol: &Ustr) -> Option<&InstrumentAny> {
        self.instruments_cache.get(symbol)
    }

    /// Processes messages and commands, returning when stopped or stream ends.
    pub async fn next(&mut self) -> Option<KrakenFuturesWsMessage> {
        // First drain any pending messages from previous ticker processing
        if let Some(msg) = self.pending_messages.pop_front() {
            return Some(msg);
        }

        loop {
            tokio::select! {
                Some(cmd) = self.cmd_rx.recv() => {
                    match cmd {
                        HandlerCommand::SetClient(client) => {
                            tracing::debug!("WebSocketClient received by futures handler");
                            self.client = Some(client);
                        }
                        HandlerCommand::SubscribeTicker(symbol) => {
                            self.send_subscribe(KrakenFuturesFeed::Ticker, &symbol).await;
                        }
                        HandlerCommand::UnsubscribeTicker(symbol) => {
                            self.send_unsubscribe(KrakenFuturesFeed::Ticker, &symbol).await;
                        }
                        HandlerCommand::SubscribeTrade(symbol) => {
                            self.send_subscribe(KrakenFuturesFeed::Trade, &symbol).await;
                        }
                        HandlerCommand::UnsubscribeTrade(symbol) => {
                            self.send_unsubscribe(KrakenFuturesFeed::Trade, &symbol).await;
                        }
                        HandlerCommand::SubscribeBook(symbol) => {
                            self.send_subscribe(KrakenFuturesFeed::Book, &symbol).await;
                        }
                        HandlerCommand::UnsubscribeBook(symbol) => {
                            self.send_unsubscribe(KrakenFuturesFeed::Book, &symbol).await;
                        }
                        HandlerCommand::Disconnect => {
                            tracing::debug!("Disconnect command received");
                            if let Some(client) = self.client.take() {
                                client.disconnect().await;
                            }
                            return None;
                        }
                        HandlerCommand::InitializeInstruments(instruments) => {
                            for inst in instruments {
                                // Key by raw_symbol (e.g., "PI_XBTUSD") since that's what
                                // WebSocket messages use
                                self.instruments_cache.insert(inst.raw_symbol().inner(), inst);
                            }
                            let count = self.instruments_cache.len();
                            tracing::debug!("Initialized {count} instruments in futures handler cache");
                        }
                        HandlerCommand::UpdateInstrument(inst) => {
                            self.instruments_cache.insert(inst.raw_symbol().inner(), inst);
                        }
                        HandlerCommand::SetAccountId(account_id) => {
                            tracing::debug!("Setting account_id for futures handler: {account_id}");
                            self.account_id = Some(account_id);
                        }
                        HandlerCommand::RequestChallenge { api_key, response_tx } => {
                            tracing::debug!("Requesting challenge for authentication");
                            self.pending_challenge_tx = Some(response_tx);
                            self.send_challenge_request(&api_key).await;
                        }
                        HandlerCommand::SetAuthCredentials { api_key, original_challenge, signed_challenge } => {
                            tracing::debug!("Setting auth credentials for futures handler");
                            self.api_key = Some(api_key);
                            self.original_challenge = Some(original_challenge);
                            self.signed_challenge = Some(signed_challenge);
                        }
                        HandlerCommand::SubscribeOpenOrders => {
                            self.send_private_subscribe(KrakenFuturesFeed::OpenOrders).await;
                        }
                        HandlerCommand::SubscribeFills => {
                            self.send_private_subscribe(KrakenFuturesFeed::Fills).await;
                        }
                        HandlerCommand::CacheClientOrder(client_order_id, instrument_id) => {
                            self.client_order_instruments.insert(client_order_id.to_string(), instrument_id);
                        }
                    }
                    continue;
                }

                msg = self.raw_rx.recv() => {
                    let msg = match msg {
                        Some(msg) => msg,
                        None => {
                            tracing::debug!("WebSocket stream closed");
                            return None;
                        }
                    };

                    if self.signal.load(Ordering::Relaxed) {
                        tracing::debug!("Stop signal received");
                        return None;
                    }

                    let text = match msg {
                        Message::Text(text) => text.to_string(),
                        Message::Binary(data) => {
                            match std::str::from_utf8(&data) {
                                Ok(s) => s.to_string(),
                                Err(_) => continue,
                            }
                        }
                        Message::Ping(data) => {
                            tracing::trace!("Received ping frame with {} bytes", data.len());
                            if let Some(client) = &self.client
                                && let Err(e) = client.send_pong(data.to_vec()).await
                            {
                                tracing::warn!(error = %e, "Failed to send pong frame");
                            }
                            continue;
                        }
                        Message::Pong(_) => {
                            tracing::trace!("Received pong");
                            continue;
                        }
                        Message::Close(_) => {
                            tracing::info!("WebSocket connection closed");
                            return None;
                        }
                        Message::Frame(_) => {
                            tracing::trace!("Received raw frame");
                            continue;
                        }
                    };

                    let ts_init = self.clock.get_time_ns();
                    self.parse_message(&text, ts_init);

                    // Return first pending message if any were produced
                    if let Some(msg) = self.pending_messages.pop_front() {
                        return Some(msg);
                    }

                    continue;
                }
            }
        }
    }

    async fn send_subscribe(&self, feed: KrakenFuturesFeed, symbol: &Symbol) {
        if let Some(ref client) = self.client {
            let feed_str = serde_json::to_string(&feed).unwrap_or_default();
            let feed_str = feed_str.trim_matches('"');
            let msg = format!(
                r#"{{"event":"subscribe","feed":"{feed_str}","product_ids":["{symbol}"]}}"#
            );
            if let Err(e) = client.send_text(msg, None).await {
                tracing::error!("Failed to send {feed:?} subscribe: {e}");
            }
        }
    }

    async fn send_unsubscribe(&self, feed: KrakenFuturesFeed, symbol: &Symbol) {
        if let Some(ref client) = self.client {
            let feed_str = serde_json::to_string(&feed).unwrap_or_default();
            let feed_str = feed_str.trim_matches('"');
            let msg = format!(
                r#"{{"event":"unsubscribe","feed":"{feed_str}","product_ids":["{symbol}"]}}"#
            );
            if let Err(e) = client.send_text(msg, None).await {
                tracing::error!("Failed to send {feed:?} unsubscribe: {e}");
            }
        }
    }

    async fn send_private_subscribe(&self, feed: KrakenFuturesFeed) {
        let Some(ref client) = self.client else {
            tracing::error!("Cannot subscribe to {feed:?}: no WebSocket client");
            return;
        };

        let Some(ref api_key) = self.api_key else {
            tracing::error!("Cannot subscribe to {feed:?}: no API key set");
            return;
        };

        let Some(ref original_challenge) = self.original_challenge else {
            tracing::error!("Cannot subscribe to {feed:?}: no challenge set");
            return;
        };

        let Some(ref signed_challenge) = self.signed_challenge else {
            tracing::error!("Cannot subscribe to {feed:?}: no signed challenge set");
            return;
        };

        let request = KrakenFuturesPrivateSubscribeRequest {
            event: KrakenFuturesEvent::Subscribe,
            feed,
            api_key: api_key.clone(),
            original_challenge: original_challenge.clone(),
            signed_challenge: signed_challenge.clone(),
        };

        let msg = match serde_json::to_string(&request) {
            Ok(m) => m,
            Err(e) => {
                tracing::error!("Failed to serialize {feed:?} subscribe request: {e}");
                return;
            }
        };

        if let Err(e) = client.send_text(msg, None).await {
            tracing::error!("Failed to send {feed:?} subscribe: {e}");
        } else {
            tracing::debug!("Sent private subscribe request for {feed:?}");
        }
    }

    async fn send_challenge_request(&self, api_key: &str) {
        let Some(ref client) = self.client else {
            tracing::error!("Cannot request challenge: no WebSocket client");
            return;
        };

        let request = KrakenFuturesChallengeRequest {
            event: KrakenFuturesEvent::Challenge,
            api_key: api_key.to_string(),
        };

        let msg = match serde_json::to_string(&request) {
            Ok(m) => m,
            Err(e) => {
                tracing::error!("Failed to serialize challenge request: {e}");
                return;
            }
        };

        if let Err(e) = client.send_text(msg, None).await {
            tracing::error!("Failed to send challenge request: {e}");
        } else {
            tracing::debug!("Sent challenge request for authentication");
        }
    }

    fn parse_message(&mut self, text: &str, ts_init: UnixNanos) {
        // Private feeds (execution)
        // Skip execution snapshots - REST reconciliation handles initial order/position state
        if text.contains("\"feed\":\"open_orders_snapshot\"") {
            tracing::debug!(
                "Skipping open_orders_snapshot (REST reconciliation handles initial state)"
            );
        } else if text.contains("\"feed\":\"open_orders\"") && text.contains("\"order\"") {
            self.handle_open_orders_delta(text, ts_init);
        } else if text.contains("\"feed\":\"fills_snapshot\"") {
            tracing::debug!("Skipping fills_snapshot (REST reconciliation handles initial state)");
        } else if text.contains("\"feed\":\"fills\"") && text.contains("\"fill_id\"") {
            self.handle_fills_delta(text, ts_init);
        }
        // Public feeds (market data)
        else if text.contains("\"feed\":\"ticker\"") && text.contains("\"product_id\"") {
            self.handle_ticker_message(text, ts_init);
        } else if text.contains("\"feed\":\"trade_snapshot\"") {
            self.handle_trade_snapshot(text, ts_init);
        } else if text.contains("\"feed\":\"trade\"") && text.contains("\"product_id\"") {
            self.handle_trade_message(text, ts_init);
        } else if text.contains("\"feed\":\"book_snapshot\"") {
            self.handle_book_snapshot(text, ts_init);
        } else if text.contains("\"feed\":\"book\"") && text.contains("\"side\"") {
            self.handle_book_delta(text, ts_init);
        } else if text.contains("\"event\":\"info\"") {
            tracing::debug!("Received info message: {text}");
        } else if text.contains("\"event\":\"pong\"") {
            tracing::trace!("Received pong response");
        } else if text.contains("\"event\":\"subscribed\"") {
            tracing::debug!("Subscription confirmed: {text}");
        } else if text.contains("\"event\":\"challenge\"") {
            self.handle_challenge_response(text);
        } else if text.contains("\"feed\":\"heartbeat\"") {
            tracing::trace!("Heartbeat received");
        } else {
            tracing::debug!("Unhandled message: {text}");
        }
    }

    fn handle_challenge_response(&mut self, text: &str) {
        // Parse the challenge response: {"event":"challenge","message":"CHALLENGE_STRING"}
        #[derive(Deserialize)]
        struct ChallengeResponse {
            message: String,
        }

        match serde_json::from_str::<ChallengeResponse>(text) {
            Ok(response) => {
                let len = response.message.len();
                tracing::debug!("Challenge received, length: {len}");

                // Send challenge back to client via oneshot channel
                if let Some(tx) = self.pending_challenge_tx.take() {
                    if tx.send(response.message).is_err() {
                        tracing::warn!("Failed to send challenge response - receiver dropped");
                    }
                } else {
                    tracing::warn!("Received challenge but no pending request");
                }
            }
            Err(e) => {
                tracing::error!("Failed to parse challenge response: {e}");
            }
        }
    }

    fn handle_ticker_message(&mut self, text: &str, ts_init: UnixNanos) {
        let ticker = match serde_json::from_str::<KrakenFuturesTickerData>(text) {
            Ok(t) => t,
            Err(e) => {
                tracing::debug!("Failed to parse ticker: {e}");
                return;
            }
        };

        // Extract instrument info upfront to avoid borrow conflicts
        let (instrument_id, price_precision) = {
            let Some(instrument) = self.get_instrument(&Ustr::from(ticker.product_id.as_str()))
            else {
                tracing::debug!("Instrument not found for product_id: {}", ticker.product_id);
                return;
            };
            (instrument.id(), instrument.price_precision())
        };

        let ts_event = ticker
            .time
            .map(|t| UnixNanos::from((t as u64) * 1_000_000))
            .unwrap_or(ts_init);

        let product_id = &ticker.product_id;
        let mark_key = format!("mark:{product_id}");
        let index_key = format!("index:{product_id}");
        let has_mark = self.subscriptions.contains(&mark_key);
        let has_index = self.subscriptions.contains(&index_key);

        // Enqueue mark price if present and subscribed
        if let Some(mark_price) = ticker.mark_price
            && has_mark
        {
            let update = MarkPriceUpdate::new(
                instrument_id,
                Price::new(mark_price, price_precision),
                ts_event,
                ts_init,
            );
            self.pending_messages
                .push_back(KrakenFuturesWsMessage::MarkPrice(update));
        }

        // Enqueue index price if present and subscribed
        if let Some(index_price) = ticker.index
            && has_index
        {
            let update = IndexPriceUpdate::new(
                instrument_id,
                Price::new(index_price, price_precision),
                ts_event,
                ts_init,
            );
            self.pending_messages
                .push_back(KrakenFuturesWsMessage::IndexPrice(update));
        }
    }

    fn handle_trade_message(&mut self, text: &str, ts_init: UnixNanos) {
        let trade = match serde_json::from_str::<KrakenFuturesTradeData>(text) {
            Ok(t) => t,
            Err(e) => {
                tracing::trace!("Failed to parse trade: {e}");
                return;
            }
        };

        // Check if subscribed to trades for this product
        let product_id = &trade.product_id;
        if !self.subscriptions.contains(&format!("trades:{product_id}")) {
            return;
        }

        // Extract instrument info upfront to avoid borrow conflicts
        let (instrument_id, price_precision, size_precision) = {
            let Some(instrument) = self.get_instrument(&Ustr::from(trade.product_id.as_str()))
            else {
                return;
            };
            (
                instrument.id(),
                instrument.price_precision(),
                instrument.size_precision(),
            )
        };

        if trade.qty == 0.0 {
            tracing::warn!("Skipping zero quantity trade for {}", trade.product_id);
            return;
        }

        let ts_event = UnixNanos::from((trade.time as u64) * 1_000_000);

        let aggressor_side = match trade.side {
            KrakenOrderSide::Buy => AggressorSide::Buyer,
            KrakenOrderSide::Sell => AggressorSide::Seller,
        };

        let trade_id = trade.uid.unwrap_or_else(|| trade.seq.to_string());

        let trade_tick = TradeTick::new(
            instrument_id,
            Price::new(trade.price, price_precision),
            Quantity::new(trade.qty, size_precision),
            aggressor_side,
            TradeId::new(&trade_id),
            ts_event,
            ts_init,
        );

        self.pending_messages
            .push_back(KrakenFuturesWsMessage::Trade(trade_tick));
    }

    fn handle_trade_snapshot(&mut self, text: &str, ts_init: UnixNanos) {
        let snapshot = match serde_json::from_str::<KrakenFuturesTradeSnapshot>(text) {
            Ok(s) => s,
            Err(e) => {
                tracing::trace!("Failed to parse trade snapshot: {e}");
                return;
            }
        };

        // Check if subscribed to trades for this product
        let product_id = &snapshot.product_id;
        if !self.subscriptions.contains(&format!("trades:{product_id}")) {
            return;
        }

        // Extract instrument info upfront
        let (instrument_id, price_precision, size_precision) = {
            let Some(instrument) = self.get_instrument(&Ustr::from(product_id.as_str())) else {
                return;
            };
            (
                instrument.id(),
                instrument.price_precision(),
                instrument.size_precision(),
            )
        };

        for trade in snapshot.trades {
            if trade.qty == 0.0 {
                tracing::warn!(
                    "Skipping zero quantity trade in snapshot for {}",
                    snapshot.product_id
                );
                continue;
            }

            let ts_event = UnixNanos::from((trade.time as u64) * 1_000_000);

            let aggressor_side = match trade.side {
                KrakenOrderSide::Buy => AggressorSide::Buyer,
                KrakenOrderSide::Sell => AggressorSide::Seller,
            };

            let trade_id = trade.uid.unwrap_or_else(|| trade.seq.to_string());

            let trade_tick = TradeTick::new(
                instrument_id,
                Price::new(trade.price, price_precision),
                Quantity::new(trade.qty, size_precision),
                aggressor_side,
                TradeId::new(&trade_id),
                ts_event,
                ts_init,
            );

            self.pending_messages
                .push_back(KrakenFuturesWsMessage::Trade(trade_tick));
        }
    }

    fn handle_book_snapshot(&mut self, text: &str, ts_init: UnixNanos) {
        let snapshot = match serde_json::from_str::<KrakenFuturesBookSnapshot>(text) {
            Ok(s) => s,
            Err(e) => {
                tracing::trace!("Failed to parse book snapshot: {e}");
                return;
            }
        };

        let product_id = &snapshot.product_id;

        // Check subscriptions
        let has_book = self.subscriptions.contains(&format!("book:{product_id}"));
        let has_quotes = self.subscriptions.contains(&format!("quotes:{product_id}"));

        if !has_book && !has_quotes {
            return;
        }

        // Extract instrument info upfront to avoid borrow conflicts
        let (instrument_id, price_precision, size_precision) = {
            let Some(instrument) = self.get_instrument(&Ustr::from(snapshot.product_id.as_str()))
            else {
                return;
            };
            (
                instrument.id(),
                instrument.price_precision(),
                instrument.size_precision(),
            )
        };

        let ts_event = UnixNanos::from((snapshot.timestamp as u64) * 1_000_000);

        let best_bid = snapshot
            .bids
            .iter()
            .filter(|l| l.qty > 0.0)
            .max_by(|a, b| a.price.total_cmp(&b.price));
        let best_ask = snapshot
            .asks
            .iter()
            .filter(|l| l.qty > 0.0)
            .min_by(|a, b| a.price.total_cmp(&b.price));

        // Emit quote if subscribed, using QuoteCache for handling partial updates
        if has_quotes {
            let bid_price = best_bid.map(|b| Price::new(b.price, price_precision));
            let ask_price = best_ask.map(|a| Price::new(a.price, price_precision));
            let bid_size = best_bid.map(|b| Quantity::new(b.qty, size_precision));
            let ask_size = best_ask.map(|a| Quantity::new(a.qty, size_precision));

            match self.quote_cache.process(
                instrument_id,
                bid_price,
                ask_price,
                bid_size,
                ask_size,
                ts_event,
                ts_init,
            ) {
                Ok(quote) => {
                    self.pending_messages
                        .push_back(KrakenFuturesWsMessage::Quote(quote));
                }
                Err(e) => {
                    tracing::trace!("Quote cache miss for {instrument_id}: {e}");
                }
            }
        }

        // Emit book deltas if subscribed
        if has_book {
            let mut deltas = Vec::new();

            // Clear action first
            deltas.push(OrderBookDelta::clear(
                instrument_id,
                snapshot.seq as u64,
                ts_event,
                ts_init,
            ));

            for level in &snapshot.bids {
                if level.qty == 0.0 {
                    continue;
                }
                let order = BookOrder::new(
                    OrderSide::Buy,
                    Price::new(level.price, price_precision),
                    Quantity::new(level.qty, size_precision),
                    0,
                );
                deltas.push(OrderBookDelta::new(
                    instrument_id,
                    BookAction::Add,
                    order,
                    0,
                    snapshot.seq as u64,
                    ts_event,
                    ts_init,
                ));
            }

            for level in &snapshot.asks {
                if level.qty == 0.0 {
                    continue;
                }
                let order = BookOrder::new(
                    OrderSide::Sell,
                    Price::new(level.price, price_precision),
                    Quantity::new(level.qty, size_precision),
                    0,
                );
                deltas.push(OrderBookDelta::new(
                    instrument_id,
                    BookAction::Add,
                    order,
                    0,
                    snapshot.seq as u64,
                    ts_event,
                    ts_init,
                ));
            }

            let book_deltas = OrderBookDeltas::new(instrument_id, deltas);
            self.pending_messages
                .push_back(KrakenFuturesWsMessage::BookDeltas(book_deltas));
        }
    }

    fn handle_book_delta(&mut self, text: &str, ts_init: UnixNanos) {
        let delta = match serde_json::from_str::<KrakenFuturesBookDelta>(text) {
            Ok(d) => d,
            Err(e) => {
                tracing::trace!("Failed to parse book delta: {e}");
                return;
            }
        };

        let product_id = &delta.product_id;

        // Check subscriptions - quotes also uses book feed
        let has_book = self.subscriptions.contains(&format!("book:{product_id}"));
        let has_quotes = self.subscriptions.contains(&format!("quotes:{product_id}"));

        // Need at least one subscription to process
        if !has_book && !has_quotes {
            return;
        }

        let Some(instrument) = self.get_instrument(&Ustr::from(delta.product_id.as_str())) else {
            return;
        };

        let ts_event = UnixNanos::from((delta.timestamp as u64) * 1_000_000);
        let instrument_id = instrument.id();
        let price_precision = instrument.price_precision();
        let size_precision = instrument.size_precision();

        let side: OrderSide = delta.side.into();

        // Emit quote update if subscribed (QuoteCache handles partial updates)
        // Note: This assumes the delta represents top-of-book, which is an approximation.
        // For accurate BBO tracking, would need to maintain full local order book.
        if has_quotes && delta.qty > 0.0 {
            let price = Price::new(delta.price, price_precision);
            let size = Quantity::new(delta.qty, size_precision);

            let (bid_price, ask_price, bid_size, ask_size) = match side {
                OrderSide::Buy => (Some(price), None, Some(size), None),
                OrderSide::Sell => (None, Some(price), None, Some(size)),
                _ => (None, None, None, None),
            };

            if let Ok(quote) = self.quote_cache.process(
                instrument_id,
                bid_price,
                ask_price,
                bid_size,
                ask_size,
                ts_event,
                ts_init,
            ) {
                self.pending_messages
                    .push_back(KrakenFuturesWsMessage::Quote(quote));
            }
        }

        // Emit book delta if subscribed
        if has_book {
            let (action, size) = if delta.qty == 0.0 {
                (BookAction::Delete, Quantity::zero(size_precision))
            } else {
                (BookAction::Update, Quantity::new(delta.qty, size_precision))
            };

            let order = BookOrder::new(side, Price::new(delta.price, price_precision), size, 0);

            let book_delta = OrderBookDelta::new(
                instrument_id,
                action,
                order,
                0,
                delta.seq as u64,
                ts_event,
                ts_init,
            );

            let book_deltas = OrderBookDeltas::new(instrument_id, vec![book_delta]);
            self.pending_messages
                .push_back(KrakenFuturesWsMessage::BookDeltas(book_deltas));
        }
    }

    #[allow(dead_code)]
    fn handle_open_orders_snapshot(&mut self, text: &str, ts_init: UnixNanos) {
        let snapshot = match serde_json::from_str::<KrakenFuturesOpenOrdersSnapshot>(text) {
            Ok(s) => s,
            Err(e) => {
                tracing::error!("Failed to parse open_orders_snapshot: {e}");
                return;
            }
        };

        let order_count = snapshot.orders.len();
        tracing::debug!("Received open_orders_snapshot with {order_count} orders");

        for order in snapshot.orders {
            if let Some(report) = self.parse_order_to_status_report(&order, ts_init, false) {
                self.pending_messages
                    .push_back(KrakenFuturesWsMessage::OrderStatusReport(Box::new(report)));
            }
        }
    }

    fn handle_open_orders_delta(&mut self, text: &str, ts_init: UnixNanos) {
        let delta = match serde_json::from_str::<KrakenFuturesOpenOrdersDelta>(text) {
            Ok(d) => d,
            Err(e) => {
                tracing::error!("Failed to parse open_orders delta: {e}");
                return;
            }
        };

        tracing::debug!(
            order_id = %delta.order.order_id,
            is_cancel = delta.is_cancel,
            reason = ?delta.reason,
            "Received open_orders delta"
        );

        if let Some(report) =
            self.parse_order_to_status_report(&delta.order, ts_init, delta.is_cancel)
        {
            self.pending_messages
                .push_back(KrakenFuturesWsMessage::OrderStatusReport(Box::new(report)));
        }
    }

    #[allow(dead_code)]
    fn handle_fills_snapshot(&mut self, text: &str, ts_init: UnixNanos) {
        let snapshot = match serde_json::from_str::<KrakenFuturesFillsSnapshot>(text) {
            Ok(s) => s,
            Err(e) => {
                tracing::error!("Failed to parse fills_snapshot: {e}");
                return;
            }
        };

        let fill_count = snapshot.fills.len();
        tracing::debug!("Received fills_snapshot with {fill_count} fills");

        for fill in snapshot.fills {
            if let Some(report) = self.parse_fill_to_report(&fill, ts_init) {
                self.pending_messages
                    .push_back(KrakenFuturesWsMessage::FillReport(Box::new(report)));
            }
        }
    }

    fn handle_fills_delta(&mut self, text: &str, ts_init: UnixNanos) {
        let delta = match serde_json::from_str::<KrakenFuturesFillsDelta>(text) {
            Ok(d) => d,
            Err(e) => {
                tracing::error!("Failed to parse fills delta: {e}");
                return;
            }
        };

        tracing::debug!(
            fill_id = %delta.fill.fill_id,
            order_id = %delta.fill.order_id,
            "Received fills delta"
        );

        if let Some(report) = self.parse_fill_to_report(&delta.fill, ts_init) {
            self.pending_messages
                .push_back(KrakenFuturesWsMessage::FillReport(Box::new(report)));
        }
    }

    fn parse_order_to_status_report(
        &self,
        order: &KrakenFuturesOpenOrder,
        ts_init: UnixNanos,
        is_cancel: bool,
    ) -> Option<OrderStatusReport> {
        let Some(account_id) = self.account_id else {
            tracing::warn!("Cannot process order: account_id not set");
            return None;
        };

        let instrument = self
            .instruments_cache
            .get(&Ustr::from(order.instrument.as_str()))?;

        let instrument_id = instrument.id();
        let size_precision = instrument.size_precision();

        let side = if order.direction == 0 {
            OrderSide::Buy
        } else {
            OrderSide::Sell
        };

        let order_type = match order.order_type.as_str() {
            "limit" | "lmt" => OrderType::Limit,
            "stop" | "stp" => OrderType::StopLimit,
            "take_profit" => OrderType::LimitIfTouched,
            "market" | "mkt" => OrderType::Market,
            _ => OrderType::Limit,
        };

        let status = if is_cancel {
            OrderStatus::Canceled
        } else if order.filled >= order.qty {
            OrderStatus::Filled
        } else if order.filled > 0.0 {
            OrderStatus::PartiallyFilled
        } else {
            OrderStatus::Accepted
        };

        if order.qty <= 0.0 {
            tracing::warn!(order_id = %order.order_id, "Skipping order with invalid quantity: {}", order.qty);
            return None;
        }

        let ts_event = UnixNanos::from((order.last_update_time as u64) * 1_000_000);

        let client_order_id = order
            .cli_ord_id
            .as_ref()
            .map(|s| ClientOrderId::new(s.as_str()));

        let filled_qty = if order.filled <= 0.0 {
            Quantity::zero(size_precision)
        } else {
            Quantity::new(order.filled, size_precision)
        };

        Some(OrderStatusReport::new(
            account_id,
            instrument_id,
            client_order_id,
            VenueOrderId::new(&order.order_id),
            side,
            order_type,
            TimeInForce::Gtc,
            status,
            Quantity::new(order.qty, size_precision),
            filled_qty,
            ts_event, // ts_accepted
            ts_event, // ts_last
            ts_init,
            Some(UUID4::new()),
        ))
    }

    fn parse_fill_to_report(
        &self,
        fill: &KrakenFuturesFill,
        ts_init: UnixNanos,
    ) -> Option<FillReport> {
        let Some(account_id) = self.account_id else {
            tracing::warn!("Cannot process fill: account_id not set");
            return None;
        };

        let instrument = self
            .instruments_cache
            .get(&Ustr::from(fill.instrument.as_str()))?;

        let instrument_id = instrument.id();
        let price_precision = instrument.price_precision();
        let size_precision = instrument.size_precision();

        if fill.qty <= 0.0 {
            tracing::warn!(fill_id = %fill.fill_id, "Skipping fill with invalid quantity: {}", fill.qty);
            return None;
        }

        let side = if fill.buy {
            OrderSide::Buy
        } else {
            OrderSide::Sell
        };

        let ts_event = UnixNanos::from((fill.time as u64) * 1_000_000);

        let client_order_id = fill
            .cli_ord_id
            .as_ref()
            .map(|s| ClientOrderId::new(s.as_str()));

        let commission = Money::new(fill.fee_paid.unwrap_or(0.0), instrument.quote_currency());

        Some(FillReport::new(
            account_id,
            instrument_id,
            VenueOrderId::new(&fill.order_id),
            TradeId::new(&fill.fill_id),
            side,
            Quantity::new(fill.qty, size_precision),
            Price::new(fill.price, price_precision),
            commission,
            LiquiditySide::NoLiquiditySide, // Not provided
            client_order_id,
            None, // venue_position_id
            ts_event,
            ts_init,
            Some(UUID4::new()),
        ))
    }
}
