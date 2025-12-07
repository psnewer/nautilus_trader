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

//! WebSocket client for the Kraken Futures v1 streaming API.

use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU8, Ordering},
};

use arc_swap::ArcSwap;
use dashmap::DashSet;
use nautilus_common::live::runtime::get_runtime;
use nautilus_model::{
    identifiers::{AccountId, ClientOrderId, InstrumentId, Symbol},
    instruments::InstrumentAny,
};
use nautilus_network::{
    mode::ConnectionMode,
    websocket::{WebSocketClient, WebSocketConfig, channel_message_handler},
};
use tokio_util::sync::CancellationToken;

use super::{
    handler::{FuturesFeedHandler, HandlerCommand},
    messages::KrakenFuturesWsMessage,
};
use crate::{common::credential::KrakenCredential, websocket::error::KrakenWsError};

const WS_PING_MSG: &str = r#"{"event":"ping"}"#;

#[derive(Debug)]
#[cfg_attr(
    feature = "python",
    pyo3::pyclass(module = "nautilus_trader.core.nautilus_pyo3.adapters")
)]
pub struct KrakenFuturesWebSocketClient {
    url: String,
    heartbeat_secs: Option<u64>,
    signal: Arc<AtomicBool>,
    connection_mode: Arc<ArcSwap<AtomicU8>>,
    cmd_tx: Arc<tokio::sync::RwLock<tokio::sync::mpsc::UnboundedSender<HandlerCommand>>>,
    out_rx: Option<Arc<tokio::sync::mpsc::UnboundedReceiver<KrakenFuturesWsMessage>>>,
    task_handle: Option<Arc<tokio::task::JoinHandle<()>>>,
    subscriptions: Arc<DashSet<String>>,
    cancellation_token: CancellationToken,
    credential: Option<KrakenCredential>,
    original_challenge: Arc<tokio::sync::RwLock<Option<String>>>,
    signed_challenge: Arc<tokio::sync::RwLock<Option<String>>>,
}

impl Clone for KrakenFuturesWebSocketClient {
    fn clone(&self) -> Self {
        Self {
            url: self.url.clone(),
            heartbeat_secs: self.heartbeat_secs,
            signal: Arc::clone(&self.signal),
            connection_mode: Arc::clone(&self.connection_mode),
            cmd_tx: Arc::clone(&self.cmd_tx),
            out_rx: self.out_rx.clone(),
            task_handle: self.task_handle.clone(),
            subscriptions: self.subscriptions.clone(),
            cancellation_token: self.cancellation_token.clone(),
            credential: self.credential.clone(),
            original_challenge: Arc::clone(&self.original_challenge),
            signed_challenge: Arc::clone(&self.signed_challenge),
        }
    }
}

impl KrakenFuturesWebSocketClient {
    #[must_use]
    pub fn new(url: String, heartbeat_secs: Option<u64>) -> Self {
        Self::with_credentials(url, heartbeat_secs, None)
    }

    /// Create a new client with API credentials for authenticated feeds.
    #[must_use]
    pub fn with_credentials(
        url: String,
        heartbeat_secs: Option<u64>,
        credential: Option<KrakenCredential>,
    ) -> Self {
        let (cmd_tx, _cmd_rx) = tokio::sync::mpsc::unbounded_channel::<HandlerCommand>();
        let initial_mode = AtomicU8::new(ConnectionMode::Closed.as_u8());
        let connection_mode = Arc::new(ArcSwap::from_pointee(initial_mode));

        Self {
            url,
            heartbeat_secs,
            signal: Arc::new(AtomicBool::new(false)),
            connection_mode,
            cmd_tx: Arc::new(tokio::sync::RwLock::new(cmd_tx)),
            out_rx: None,
            task_handle: None,
            subscriptions: Arc::new(DashSet::new()),
            cancellation_token: CancellationToken::new(),
            credential,
            original_challenge: Arc::new(tokio::sync::RwLock::new(None)),
            signed_challenge: Arc::new(tokio::sync::RwLock::new(None)),
        }
    }

    /// Returns true if the client has API credentials set.
    #[must_use]
    pub fn has_credentials(&self) -> bool {
        self.credential.is_some()
    }

    #[must_use]
    pub fn url(&self) -> &str {
        &self.url
    }

    #[must_use]
    pub fn is_closed(&self) -> bool {
        ConnectionMode::from_u8(self.connection_mode.load().load(Ordering::Relaxed))
            == ConnectionMode::Closed
    }

    #[must_use]
    pub fn is_active(&self) -> bool {
        ConnectionMode::from_u8(self.connection_mode.load().load(Ordering::Relaxed))
            == ConnectionMode::Active
    }

    /// Wait until the WebSocket connection is active.
    pub async fn wait_until_active(&self, timeout_secs: f64) -> Result<(), KrakenWsError> {
        let timeout = tokio::time::Duration::from_secs_f64(timeout_secs);

        tokio::time::timeout(timeout, async {
            while !self.is_active() {
                tokio::time::sleep(tokio::time::Duration::from_millis(10)).await;
            }
        })
        .await
        .map_err(|_| {
            KrakenWsError::ConnectionError(format!(
                "WebSocket connection timeout after {timeout_secs} seconds"
            ))
        })?;

        Ok(())
    }

    /// Authenticate the WebSocket connection for private feeds.
    ///
    /// This sends a challenge request, waits for the response, signs it,
    /// and stores the credentials for use in private subscriptions.
    pub async fn authenticate(&self) -> Result<(), KrakenWsError> {
        let credential = self.credential.as_ref().ok_or_else(|| {
            KrakenWsError::AuthenticationError("API credentials required".to_string())
        })?;

        let api_key = credential.api_key().to_string();
        let (tx, rx) = tokio::sync::oneshot::channel();

        self.cmd_tx
            .read()
            .await
            .send(HandlerCommand::RequestChallenge {
                api_key: api_key.clone(),
                response_tx: tx,
            })
            .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;

        let challenge = tokio::time::timeout(tokio::time::Duration::from_secs(10), rx)
            .await
            .map_err(|_| {
                KrakenWsError::AuthenticationError("Timeout waiting for challenge".to_string())
            })?
            .map_err(|_| {
                KrakenWsError::AuthenticationError("Challenge channel closed".to_string())
            })?;

        let signed_challenge = credential.sign_ws_challenge(&challenge).map_err(|e| {
            KrakenWsError::AuthenticationError(format!("Failed to sign challenge: {e}"))
        })?;

        *self.original_challenge.write().await = Some(challenge.clone());
        *self.signed_challenge.write().await = Some(signed_challenge.clone());

        self.cmd_tx
            .read()
            .await
            .send(HandlerCommand::SetAuthCredentials {
                api_key,
                original_challenge: challenge,
                signed_challenge,
            })
            .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;

        tracing::debug!("Futures WebSocket authentication successful");
        Ok(())
    }

    /// Cache instruments for price precision lookup (bulk replace).
    ///
    /// Must be called after `connect()` when the handler is ready to receive commands.
    pub fn cache_instruments(&self, instruments: Vec<InstrumentAny>) {
        if let Ok(tx) = self.cmd_tx.try_read()
            && let Err(e) = tx.send(HandlerCommand::InitializeInstruments(instruments))
        {
            tracing::debug!("Failed to send instruments to handler: {e}");
        }
    }

    /// Cache a single instrument for price precision lookup (upsert).
    ///
    /// Must be called after `connect()` when the handler is ready to receive commands.
    pub fn cache_instrument(&self, instrument: InstrumentAny) {
        if let Ok(tx) = self.cmd_tx.try_read()
            && let Err(e) = tx.send(HandlerCommand::UpdateInstrument(instrument))
        {
            tracing::debug!("Failed to send instrument update to handler: {e}");
        }
    }

    pub async fn connect(&mut self) -> Result<(), KrakenWsError> {
        tracing::debug!("Connecting to Futures WebSocket: {}", self.url);

        self.signal.store(false, Ordering::Relaxed);

        let (raw_handler, raw_rx) = channel_message_handler();

        let ws_config = WebSocketConfig {
            url: self.url.clone(),
            headers: vec![],
            message_handler: Some(raw_handler),
            ping_handler: None,
            heartbeat: self.heartbeat_secs,
            heartbeat_msg: Some(WS_PING_MSG.to_string()),
            reconnect_timeout_ms: None,
            reconnect_delay_initial_ms: None,
            reconnect_delay_max_ms: None,
            reconnect_backoff_factor: None,
            reconnect_jitter_ms: None,
            reconnect_max_attempts: None,
        };

        let ws_client = WebSocketClient::connect(ws_config, None, vec![], None)
            .await
            .map_err(|e| KrakenWsError::ConnectionError(e.to_string()))?;

        self.connection_mode
            .store(ws_client.connection_mode_atomic());

        let (out_tx, out_rx) = tokio::sync::mpsc::unbounded_channel::<KrakenFuturesWsMessage>();
        self.out_rx = Some(Arc::new(out_rx));

        let (cmd_tx, cmd_rx) = tokio::sync::mpsc::unbounded_channel::<HandlerCommand>();
        *self.cmd_tx.write().await = cmd_tx.clone();

        if let Err(e) = cmd_tx.send(HandlerCommand::SetClient(ws_client)) {
            return Err(KrakenWsError::ConnectionError(format!(
                "Failed to send WebSocketClient to handler: {e}"
            )));
        }

        let signal = self.signal.clone();
        let subscriptions = self.subscriptions.clone();

        let stream_handle = get_runtime().spawn(async move {
            let mut handler = FuturesFeedHandler::new(signal, cmd_rx, raw_rx, subscriptions);

            while let Some(msg) = handler.next().await {
                if let Err(e) = out_tx.send(msg) {
                    tracing::debug!("Output channel closed: {e}");
                    break;
                }
            }

            tracing::debug!("Futures handler task exiting");
        });

        self.task_handle = Some(Arc::new(stream_handle));

        tracing::debug!("Futures WebSocket connected successfully");
        Ok(())
    }

    pub async fn disconnect(&mut self) -> Result<(), KrakenWsError> {
        tracing::debug!("Disconnecting Futures WebSocket");

        self.signal.store(true, Ordering::Relaxed);

        if let Err(e) = self.cmd_tx.read().await.send(HandlerCommand::Disconnect) {
            tracing::debug!(
                "Failed to send disconnect command (handler may already be shut down): {e}"
            );
        }

        if let Some(task_handle) = self.task_handle.take() {
            match Arc::try_unwrap(task_handle) {
                Ok(handle) => {
                    match tokio::time::timeout(tokio::time::Duration::from_secs(2), handle).await {
                        Ok(Ok(())) => tracing::debug!("Task handle completed successfully"),
                        Ok(Err(e)) => tracing::error!("Task handle encountered an error: {e:?}"),
                        Err(_) => {
                            tracing::warn!("Timeout waiting for task handle");
                        }
                    }
                }
                Err(arc_handle) => {
                    tracing::debug!("Cannot take ownership of task handle, aborting");
                    arc_handle.abort();
                }
            }
        }

        self.subscriptions.clear();
        Ok(())
    }

    pub async fn close(&mut self) -> Result<(), KrakenWsError> {
        self.disconnect().await
    }

    /// Subscribe to mark price updates for the given instrument.
    pub async fn subscribe_mark_price(
        &self,
        instrument_id: InstrumentId,
    ) -> Result<(), KrakenWsError> {
        let symbol = instrument_id.symbol;
        let key = format!("mark:{symbol}");
        if self.subscriptions.contains(&key) {
            return Ok(());
        }

        self.subscriptions.insert(key);
        self.ensure_ticker_subscribed(symbol).await
    }

    /// Unsubscribe from mark price updates for the given instrument.
    pub async fn unsubscribe_mark_price(
        &self,
        instrument_id: InstrumentId,
    ) -> Result<(), KrakenWsError> {
        let symbol = instrument_id.symbol;
        self.subscriptions.remove(&format!("mark:{symbol}"));
        self.maybe_unsubscribe_ticker(symbol).await
    }

    /// Subscribe to index price updates for the given instrument.
    pub async fn subscribe_index_price(
        &self,
        instrument_id: InstrumentId,
    ) -> Result<(), KrakenWsError> {
        let symbol = instrument_id.symbol;
        let key = format!("index:{symbol}");
        if self.subscriptions.contains(&key) {
            return Ok(());
        }

        self.subscriptions.insert(key);
        self.ensure_ticker_subscribed(symbol).await
    }

    /// Unsubscribe from index price updates for the given instrument.
    pub async fn unsubscribe_index_price(
        &self,
        instrument_id: InstrumentId,
    ) -> Result<(), KrakenWsError> {
        let symbol = instrument_id.symbol;
        self.subscriptions.remove(&format!("index:{symbol}"));
        self.maybe_unsubscribe_ticker(symbol).await
    }

    /// Subscribe to quote updates for the given instrument.
    ///
    /// Uses the order book channel for low-latency top-of-book quotes.
    pub async fn subscribe_quotes(&self, instrument_id: InstrumentId) -> Result<(), KrakenWsError> {
        let symbol = instrument_id.symbol;
        let key = format!("quotes:{symbol}");
        if self.subscriptions.contains(&key) {
            return Ok(());
        }

        self.subscriptions.insert(key);

        // Use book feed for low-latency quotes (not throttled ticker)
        self.ensure_book_subscribed(symbol).await
    }

    /// Unsubscribe from quote updates for the given instrument.
    pub async fn unsubscribe_quotes(
        &self,
        instrument_id: InstrumentId,
    ) -> Result<(), KrakenWsError> {
        let symbol = instrument_id.symbol;
        self.subscriptions.remove(&format!("quotes:{symbol}"));
        self.maybe_unsubscribe_book(symbol).await
    }

    /// Subscribe to trade updates for the given instrument.
    pub async fn subscribe_trades(&self, instrument_id: InstrumentId) -> Result<(), KrakenWsError> {
        let symbol = instrument_id.symbol;
        let key = format!("trades:{symbol}");
        if self.subscriptions.contains(&key) {
            return Ok(());
        }

        self.subscriptions.insert(key.clone());

        self.cmd_tx
            .read()
            .await
            .send(HandlerCommand::SubscribeTrade(symbol))
            .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;

        Ok(())
    }

    /// Unsubscribe from trade updates for the given instrument.
    pub async fn unsubscribe_trades(
        &self,
        instrument_id: InstrumentId,
    ) -> Result<(), KrakenWsError> {
        let symbol = instrument_id.symbol;
        let key = format!("trades:{symbol}");
        if !self.subscriptions.contains(&key) {
            return Ok(());
        }

        self.subscriptions.remove(&key);

        self.cmd_tx
            .read()
            .await
            .send(HandlerCommand::UnsubscribeTrade(symbol))
            .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;

        Ok(())
    }

    /// Subscribe to order book updates for the given instrument.
    ///
    /// Note: The `depth` parameter is accepted for API compatibility with spot client but is
    /// not used by Kraken Futures (full book is always returned).
    pub async fn subscribe_book(
        &self,
        instrument_id: InstrumentId,
        _depth: Option<u32>,
    ) -> Result<(), KrakenWsError> {
        let symbol = instrument_id.symbol;
        let key = format!("book:{symbol}");
        if self.subscriptions.contains(&key) {
            return Ok(());
        }

        self.subscriptions.insert(key.clone());

        self.cmd_tx
            .read()
            .await
            .send(HandlerCommand::SubscribeBook(symbol))
            .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;

        Ok(())
    }

    /// Unsubscribe from order book updates for the given instrument.
    pub async fn unsubscribe_book(&self, instrument_id: InstrumentId) -> Result<(), KrakenWsError> {
        let symbol = instrument_id.symbol;
        let key = format!("book:{symbol}");
        if !self.subscriptions.contains(&key) {
            return Ok(());
        }

        self.subscriptions.remove(&key);

        self.cmd_tx
            .read()
            .await
            .send(HandlerCommand::UnsubscribeBook(symbol))
            .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;

        Ok(())
    }

    /// Ensure ticker feed is subscribed for the given symbol.
    async fn ensure_ticker_subscribed(&self, symbol: Symbol) -> Result<(), KrakenWsError> {
        let ticker_key = format!("ticker:{symbol}");
        if !self.subscriptions.contains(&ticker_key) {
            self.subscriptions.insert(ticker_key);
            self.cmd_tx
                .read()
                .await
                .send(HandlerCommand::SubscribeTicker(symbol))
                .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;
        }
        Ok(())
    }

    /// Unsubscribe from ticker if no more dependent subscriptions.
    async fn maybe_unsubscribe_ticker(&self, symbol: Symbol) -> Result<(), KrakenWsError> {
        let has_mark = self.subscriptions.contains(&format!("mark:{symbol}"));
        let has_index = self.subscriptions.contains(&format!("index:{symbol}"));

        if !has_mark && !has_index {
            self.subscriptions.remove(&format!("ticker:{symbol}"));
            self.cmd_tx
                .read()
                .await
                .send(HandlerCommand::UnsubscribeTicker(symbol))
                .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;
        }
        Ok(())
    }

    /// Ensure book feed is subscribed for the given symbol (for quotes).
    async fn ensure_book_subscribed(&self, symbol: Symbol) -> Result<(), KrakenWsError> {
        let book_key = format!("book:{symbol}");
        if !self.subscriptions.contains(&book_key) {
            self.subscriptions.insert(book_key);
            self.cmd_tx
                .read()
                .await
                .send(HandlerCommand::SubscribeBook(symbol))
                .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;
        }
        Ok(())
    }

    /// Unsubscribe from book if no more dependent subscriptions.
    async fn maybe_unsubscribe_book(&self, symbol: Symbol) -> Result<(), KrakenWsError> {
        let has_quotes = self.subscriptions.contains(&format!("quotes:{symbol}"));
        let has_book = self.subscriptions.contains(&format!("book:{symbol}"));

        // Only unsubscribe if no quotes subscription and no explicit book subscription
        if !has_quotes && !has_book {
            self.subscriptions.remove(&format!("book:{symbol}"));
            self.cmd_tx
                .read()
                .await
                .send(HandlerCommand::UnsubscribeBook(symbol))
                .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;
        }
        Ok(())
    }

    /// Get the output receiver for processed messages.
    pub fn take_output_rx(
        &mut self,
    ) -> Option<tokio::sync::mpsc::UnboundedReceiver<KrakenFuturesWsMessage>> {
        self.out_rx.take().and_then(|arc| Arc::try_unwrap(arc).ok())
    }

    /// Sets the account ID for execution reports.
    ///
    /// Must be called before subscribing to execution feeds to properly generate
    /// OrderStatusReport and FillReport objects.
    pub fn set_account_id(&self, account_id: AccountId) {
        if let Ok(tx) = self.cmd_tx.try_read()
            && let Err(e) = tx.send(HandlerCommand::SetAccountId(account_id))
        {
            tracing::debug!("Failed to send account_id to handler: {e}");
        }
    }

    /// Cache a client order ID to instrument ID mapping for order tracking.
    ///
    /// This helps the handler resolve instrument info when WebSocket messages
    /// arrive before HTTP responses.
    pub fn cache_client_order(&self, client_order_id: ClientOrderId, instrument_id: InstrumentId) {
        if let Ok(tx) = self.cmd_tx.try_read()
            && let Err(e) = tx.send(HandlerCommand::CacheClientOrder(
                client_order_id,
                instrument_id,
            ))
        {
            tracing::debug!("Failed to cache client order: {e}");
        }
    }

    /// Request a challenge from the WebSocket for authentication.
    ///
    /// After calling this, listen for the challenge response message and then
    /// call `authenticate_with_challenge()` to complete authentication.
    pub async fn request_challenge(&self) -> Result<(), KrakenWsError> {
        let credential = self.credential.as_ref().ok_or_else(|| {
            KrakenWsError::AuthenticationError(
                "API credentials required for authentication".to_string(),
            )
        })?;

        // TODO: Send via WebSocket client when we have direct access
        // For now, the Python layer will handle the challenge request/response flow
        tracing::debug!(
            "Challenge request prepared for API key: {}",
            credential.api_key_masked()
        );

        Ok(())
    }

    /// Set authentication credentials directly (for when challenge is obtained externally).
    pub async fn set_auth_credentials(
        &self,
        original_challenge: String,
        signed_challenge: String,
    ) -> Result<(), KrakenWsError> {
        let credential = self.credential.as_ref().ok_or_else(|| {
            KrakenWsError::AuthenticationError("API credentials required".to_string())
        })?;

        *self.original_challenge.write().await = Some(original_challenge.clone());
        *self.signed_challenge.write().await = Some(signed_challenge.clone());

        self.cmd_tx
            .read()
            .await
            .send(HandlerCommand::SetAuthCredentials {
                api_key: credential.api_key().to_string(),
                original_challenge,
                signed_challenge,
            })
            .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;

        Ok(())
    }

    /// Sign a challenge with the API credentials.
    ///
    /// Returns the signed challenge on success.
    pub fn sign_challenge(&self, challenge: &str) -> Result<String, KrakenWsError> {
        let credential = self.credential.as_ref().ok_or_else(|| {
            KrakenWsError::AuthenticationError("API credentials required".to_string())
        })?;

        credential.sign_ws_challenge(challenge).map_err(|e| {
            KrakenWsError::AuthenticationError(format!("Failed to sign challenge: {e}"))
        })
    }

    /// Complete authentication with a received challenge.
    pub async fn authenticate_with_challenge(&self, challenge: &str) -> Result<(), KrakenWsError> {
        let credential = self.credential.as_ref().ok_or_else(|| {
            KrakenWsError::AuthenticationError("API credentials required".to_string())
        })?;

        let signed_challenge = credential.sign_ws_challenge(challenge).map_err(|e| {
            KrakenWsError::AuthenticationError(format!("Failed to sign challenge: {e}"))
        })?;

        self.set_auth_credentials(challenge.to_string(), signed_challenge)
            .await
    }

    /// Subscribe to open orders feed (private, requires authentication).
    pub async fn subscribe_open_orders(&self) -> Result<(), KrakenWsError> {
        if self.original_challenge.read().await.is_none() {
            return Err(KrakenWsError::AuthenticationError(
                "Must authenticate before subscribing to private feeds".to_string(),
            ));
        }

        self.subscriptions.insert("open_orders".to_string());

        self.cmd_tx
            .read()
            .await
            .send(HandlerCommand::SubscribeOpenOrders)
            .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;

        Ok(())
    }

    /// Subscribe to fills feed (private, requires authentication).
    pub async fn subscribe_fills(&self) -> Result<(), KrakenWsError> {
        if self.original_challenge.read().await.is_none() {
            return Err(KrakenWsError::AuthenticationError(
                "Must authenticate before subscribing to private feeds".to_string(),
            ));
        }

        self.subscriptions.insert("fills".to_string());

        self.cmd_tx
            .read()
            .await
            .send(HandlerCommand::SubscribeFills)
            .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;

        Ok(())
    }

    /// Subscribe to both open orders and fills (convenience method).
    pub async fn subscribe_executions(&self) -> Result<(), KrakenWsError> {
        self.subscribe_open_orders().await?;
        self.subscribe_fills().await?;
        Ok(())
    }
}
