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
    Arc, RwLock,
    atomic::{AtomicBool, AtomicU8, Ordering},
};

use ahash::AHashMap;
use arc_swap::ArcSwap;
use dashmap::DashSet;
use nautilus_common::runtime::get_runtime;
use nautilus_core::{nanos::UnixNanos, time::get_atomic_clock_realtime};
use nautilus_model::{
    data::{IndexPriceUpdate, MarkPriceUpdate},
    instruments::{Instrument, InstrumentAny},
    types::Price,
};
use nautilus_network::{
    mode::ConnectionMode,
    websocket::{WebSocketClient, WebSocketConfig, channel_message_handler},
};
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;
use tokio_util::sync::CancellationToken;
use ustr::Ustr;

use super::messages::KrakenFuturesTickerData;
use crate::websocket::error::KrakenWsError;

/// Output message types from the Futures WebSocket handler.
#[derive(Clone, Debug)]
pub enum KrakenFuturesWsMessage {
    MarkPrice(MarkPriceUpdate),
    IndexPrice(IndexPriceUpdate),
}

/// Commands for the Futures WebSocket handler.
enum FuturesHandlerCommand {
    SetClient(WebSocketClient),
    Subscribe(String),
    Unsubscribe(String),
    Disconnect,
}

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
    cmd_tx: Arc<tokio::sync::RwLock<mpsc::UnboundedSender<FuturesHandlerCommand>>>,
    out_rx: Option<Arc<mpsc::UnboundedReceiver<KrakenFuturesWsMessage>>>,
    task_handle: Option<Arc<tokio::task::JoinHandle<()>>>,
    subscriptions: Arc<DashSet<String>>,
    cancellation_token: CancellationToken,
    /// Cache of instruments keyed by raw symbol (e.g., "PI_XBTUSD")
    instruments_cache: Arc<RwLock<AHashMap<Ustr, InstrumentAny>>>,
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
            instruments_cache: Arc::clone(&self.instruments_cache),
        }
    }
}

impl KrakenFuturesWebSocketClient {
    #[must_use]
    pub fn new(url: String, heartbeat_secs: Option<u64>) -> Self {
        let (cmd_tx, _cmd_rx) = mpsc::unbounded_channel::<FuturesHandlerCommand>();
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
            instruments_cache: Arc::new(RwLock::new(AHashMap::new())),
        }
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

    /// Cache instruments for price precision lookup.
    pub fn cache_instruments(&self, instruments: Vec<InstrumentAny>) {
        // TODO: Implement standard handler pattern (will remove this mutex)
        // SAFETY: Lock should not be poisoned in normal operation
        let mut cache = self.instruments_cache.write().expect("Lock poisoned");
        for inst in instruments {
            // Key by raw_symbol (e.g., "PI_XBTUSD") since that's what WebSocket messages use
            cache.insert(inst.raw_symbol().inner(), inst);
        }
    }

    pub async fn connect(&mut self) -> Result<(), KrakenWsError> {
        tracing::debug!("Connecting to Futures WebSocket: {}", self.url);

        self.signal.store(false, Ordering::Relaxed);

        let (raw_handler, mut raw_rx) = channel_message_handler();

        let ws_config = WebSocketConfig {
            url: self.url.clone(),
            headers: vec![],
            message_handler: Some(raw_handler),
            ping_handler: None,
            heartbeat: self.heartbeat_secs,
            heartbeat_msg: None, // Futures uses heartbeat feed, not ping
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

        let (out_tx, out_rx) = mpsc::unbounded_channel::<KrakenFuturesWsMessage>();
        self.out_rx = Some(Arc::new(out_rx));

        let (cmd_tx, mut cmd_rx) = mpsc::unbounded_channel::<FuturesHandlerCommand>();
        *self.cmd_tx.write().await = cmd_tx.clone();

        if let Err(e) = cmd_tx.send(FuturesHandlerCommand::SetClient(ws_client)) {
            return Err(KrakenWsError::ConnectionError(format!(
                "Failed to send WebSocketClient to handler: {e}"
            )));
        }

        let signal = self.signal.clone();
        let subscriptions = self.subscriptions.clone();
        let instruments_cache: Arc<RwLock<AHashMap<Ustr, InstrumentAny>>> =
            Arc::clone(&self.instruments_cache);

        let stream_handle = get_runtime().spawn(async move {
            let mut ws_client: Option<WebSocketClient> = None;

            loop {
                tokio::select! {
                    Some(cmd) = cmd_rx.recv() => {
                        match cmd {
                            FuturesHandlerCommand::SetClient(client) => {
                                ws_client = Some(client);
                            }
                            FuturesHandlerCommand::Subscribe(product_id) => {
                                if let Some(ref client) = ws_client {
                                    let msg = format!(
                                        r#"{{"event":"subscribe","feed":"ticker","product_ids":["{}"]}}"#,
                                        product_id
                                    );
                                    if let Err(e) = client.send_text(msg, None).await {
                                        tracing::error!("Failed to send subscribe: {e}");
                                    }
                                }
                            }
                            FuturesHandlerCommand::Unsubscribe(product_id) => {
                                if let Some(ref client) = ws_client {
                                    let msg = format!(
                                        r#"{{"event":"unsubscribe","feed":"ticker","product_ids":["{}"]}}"#,
                                        product_id
                                    );
                                    if let Err(e) = client.send_text(msg, None).await {
                                        tracing::error!("Failed to send unsubscribe: {e}");
                                    }
                                }
                            }
                            FuturesHandlerCommand::Disconnect => {
                                tracing::debug!("Disconnect command received");
                                if let Some(ref client) = ws_client {
                                    let _ = client.disconnect().await;
                                }
                                break;
                            }
                        }
                    }
                    Some(raw_msg) = raw_rx.recv() => {
                        if signal.load(Ordering::Relaxed) {
                            break;
                        }

                        let text = match raw_msg {
                            Message::Text(text) => text.to_string(),
                            Message::Binary(data) => {
                                match std::str::from_utf8(&data) {
                                    Ok(s) => s.to_string(),
                                    Err(_) => continue,
                                }
                            }
                            _ => continue,
                        };

                        // Check if it's a ticker message
                        if text.contains("\"feed\":\"ticker\"") && text.contains("\"product_id\"") {
                            match serde_json::from_str::<KrakenFuturesTickerData>(&text) {
                                Ok(ticker) => {
                                    // Look up instrument from cache for price precision
                                    // SAFETY: Lock should not be poisoned in normal operation
                                    let instrument = {
                                        let cache = instruments_cache.read().expect("Lock poisoned");
                                        cache.get(&Ustr::from(ticker.product_id.as_str())).cloned()
                                    };

                                    let Some(instrument) = instrument else {
                                        tracing::debug!(
                                            "Instrument {} not in cache, skipping ticker update",
                                            ticker.product_id
                                        );
                                        continue;
                                    };

                                    let ts_init = get_atomic_clock_realtime().get_time_ns();
                                    let ts_event = ticker.time
                                        .map(|t| UnixNanos::from((t as u64) * 1_000_000))
                                        .unwrap_or(ts_init);

                                    let instrument_id = instrument.id();
                                    let price_precision = instrument.price_precision();

                                    // Emit mark price if present and subscribed
                                    if let Some(mark_price) = ticker.mark_price
                                        && subscriptions.contains(&format!("mark:{}", ticker.product_id))
                                    {
                                        let update = MarkPriceUpdate::new(
                                            instrument_id,
                                            Price::new(mark_price, price_precision),
                                            ts_event,
                                            ts_init,
                                        );
                                        let _ = out_tx.send(KrakenFuturesWsMessage::MarkPrice(update));
                                    }

                                    // Emit index price if present and subscribed
                                    if let Some(index_price) = ticker.index
                                        && subscriptions.contains(&format!("index:{}", ticker.product_id))
                                    {
                                        let update = IndexPriceUpdate::new(
                                            instrument_id,
                                            Price::new(index_price, price_precision),
                                            ts_event,
                                            ts_init,
                                        );
                                        let _ = out_tx.send(KrakenFuturesWsMessage::IndexPrice(update));
                                    }
                                }
                                Err(e) => {
                                    tracing::trace!("Failed to parse ticker: {e}");
                                }
                            }
                        } else if text.contains("\"event\":\"info\"") {
                            tracing::debug!("Received info message: {text}");
                        } else if text.contains("\"event\":\"subscribed\"") {
                            tracing::debug!("Subscription confirmed: {text}");
                        } else if text.contains("\"feed\":\"heartbeat\"") {
                            tracing::trace!("Heartbeat received");
                        }
                    }
                    else => break,
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

        if let Err(e) = self
            .cmd_tx
            .read()
            .await
            .send(FuturesHandlerCommand::Disconnect)
        {
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

    /// Subscribe to mark price updates for the given product.
    pub async fn subscribe_mark_price(&self, product_id: &str) -> Result<(), KrakenWsError> {
        let key = format!("mark:{product_id}");
        if self.subscriptions.contains(&key) {
            return Ok(());
        }

        self.subscriptions.insert(key);

        // Only subscribe to ticker feed if not already subscribed for this product
        let ticker_key = format!("ticker:{product_id}");
        if !self.subscriptions.contains(&ticker_key) {
            self.subscriptions.insert(ticker_key);
            self.cmd_tx
                .read()
                .await
                .send(FuturesHandlerCommand::Subscribe(product_id.to_string()))
                .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;
        }

        Ok(())
    }

    /// Unsubscribe from mark price updates for the given product.
    pub async fn unsubscribe_mark_price(&self, product_id: &str) -> Result<(), KrakenWsError> {
        self.subscriptions.remove(&format!("mark:{product_id}"));

        // Only unsubscribe from ticker if no more mark/index subscriptions
        let has_mark = self.subscriptions.contains(&format!("mark:{product_id}"));
        let has_index = self.subscriptions.contains(&format!("index:{product_id}"));

        if !has_mark && !has_index {
            self.subscriptions.remove(&format!("ticker:{product_id}"));
            self.cmd_tx
                .read()
                .await
                .send(FuturesHandlerCommand::Unsubscribe(product_id.to_string()))
                .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;
        }

        Ok(())
    }

    /// Subscribe to index price updates for the given product.
    pub async fn subscribe_index_price(&self, product_id: &str) -> Result<(), KrakenWsError> {
        let key = format!("index:{product_id}");
        if self.subscriptions.contains(&key) {
            return Ok(());
        }

        self.subscriptions.insert(key);

        // Only subscribe to ticker feed if not already subscribed for this product
        let ticker_key = format!("ticker:{product_id}");
        if !self.subscriptions.contains(&ticker_key) {
            self.subscriptions.insert(ticker_key);
            self.cmd_tx
                .read()
                .await
                .send(FuturesHandlerCommand::Subscribe(product_id.to_string()))
                .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;
        }

        Ok(())
    }

    /// Unsubscribe from index price updates for the given product.
    pub async fn unsubscribe_index_price(&self, product_id: &str) -> Result<(), KrakenWsError> {
        self.subscriptions.remove(&format!("index:{product_id}"));

        // Only unsubscribe from ticker if no more mark/index subscriptions
        let has_mark = self.subscriptions.contains(&format!("mark:{product_id}"));
        let has_index = self.subscriptions.contains(&format!("index:{product_id}"));

        if !has_mark && !has_index {
            self.subscriptions.remove(&format!("ticker:{product_id}"));
            self.cmd_tx
                .read()
                .await
                .send(FuturesHandlerCommand::Unsubscribe(product_id.to_string()))
                .map_err(|e| KrakenWsError::ChannelError(e.to_string()))?;
        }

        Ok(())
    }

    /// Get the output receiver for processed messages.
    pub fn take_output_rx(&mut self) -> Option<mpsc::UnboundedReceiver<KrakenFuturesWsMessage>> {
        self.out_rx.take().and_then(|arc| Arc::try_unwrap(arc).ok())
    }
}
