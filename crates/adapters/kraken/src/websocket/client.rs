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

//! WebSocket client for the Kraken v2 streaming API.

use std::sync::Arc;

use dashmap::DashMap;
use nautilus_network::websocket::{WebSocketClient, WebSocketConfig, channel_message_handler};
use tokio::sync::RwLock;
use tokio_tungstenite::tungstenite::Message;
use tokio_util::sync::CancellationToken;
use ustr::Ustr;

use super::{
    enums::{KrakenWsChannel, KrakenWsMethod},
    error::KrakenWsError,
    messages::{KrakenWsParams, KrakenWsRequest},
};
use crate::config::KrakenDataClientConfig;

pub struct KrakenWebSocketClient {
    url: String,
    config: KrakenDataClientConfig,
    ws_client: Option<Arc<WebSocketClient>>,
    raw_rx: Option<tokio::sync::mpsc::UnboundedReceiver<Message>>,
    subscriptions: Arc<DashMap<String, KrakenWsChannel>>,
    #[allow(dead_code)]
    cancellation_token: CancellationToken,
    req_id_counter: Arc<RwLock<u64>>,
}

impl KrakenWebSocketClient {
    pub fn new(config: KrakenDataClientConfig, cancellation_token: CancellationToken) -> Self {
        let url = config.ws_public_url();

        Self {
            url,
            config,
            ws_client: None,
            raw_rx: None,
            subscriptions: Arc::new(DashMap::new()),
            cancellation_token,
            req_id_counter: Arc::new(RwLock::new(0)),
        }
    }

    async fn get_next_req_id(&self) -> u64 {
        let mut counter = self.req_id_counter.write().await;
        *counter += 1;
        *counter
    }

    pub async fn connect(&mut self) -> Result<(), KrakenWsError> {
        tracing::debug!("Connecting to {}", self.url);

        let (raw_handler, raw_rx) = channel_message_handler();

        let ws_config = WebSocketConfig {
            url: self.url.clone(),
            headers: vec![],
            message_handler: Some(raw_handler),
            ping_handler: None,
            heartbeat: self.config.heartbeat_interval_secs,
            heartbeat_msg: Some("ping".to_string()),
            reconnect_timeout_ms: None,
            reconnect_delay_initial_ms: None,
            reconnect_delay_max_ms: None,
            reconnect_backoff_factor: None,
            reconnect_jitter_ms: None,
        };

        let ws_client = WebSocketClient::connect(
            ws_config,
            None,   // post_reconnection
            vec![], // keyed_quotas
            None,   // default_quota
        )
        .await
        .map_err(|e| KrakenWsError::ConnectionError(e.to_string()))?;

        self.ws_client = Some(Arc::new(ws_client));
        self.raw_rx = Some(raw_rx);

        tracing::debug!("WebSocket connected successfully");
        Ok(())
    }

    pub async fn disconnect(&mut self) -> Result<(), KrakenWsError> {
        tracing::debug!("Disconnecting WebSocket");

        if let Some(ws_client) = self.ws_client.take() {
            ws_client.disconnect().await;
        }

        self.subscriptions.clear();

        Ok(())
    }

    pub async fn subscribe(
        &self,
        channel: KrakenWsChannel,
        symbols: Vec<Ustr>,
    ) -> Result<(), KrakenWsError> {
        let req_id = self.get_next_req_id().await;

        let request = KrakenWsRequest {
            method: KrakenWsMethod::Subscribe,
            params: Some(KrakenWsParams {
                channel,
                symbol: Some(symbols.clone()),
                snapshot: None,
                depth: None,
                token: None,
            }),
            req_id: Some(req_id),
        };

        self.send_request(&request).await?;

        // Track subscriptions
        for symbol in symbols {
            let key = format!("{:?}:{}", channel, symbol);
            self.subscriptions.insert(key, channel);
        }

        Ok(())
    }

    pub async fn unsubscribe(
        &self,
        channel: KrakenWsChannel,
        symbols: Vec<Ustr>,
    ) -> Result<(), KrakenWsError> {
        let req_id = self.get_next_req_id().await;

        let request = KrakenWsRequest {
            method: KrakenWsMethod::Unsubscribe,
            params: Some(KrakenWsParams {
                channel,
                symbol: Some(symbols.clone()),
                snapshot: None,
                depth: None,
                token: None,
            }),
            req_id: Some(req_id),
        };

        self.send_request(&request).await?;

        // Remove from subscriptions
        for symbol in symbols {
            let key = format!("{:?}:{}", channel, symbol);
            self.subscriptions.remove(&key);
        }

        Ok(())
    }

    pub async fn send_ping(&self) -> Result<(), KrakenWsError> {
        let req_id = self.get_next_req_id().await;

        let request = KrakenWsRequest {
            method: KrakenWsMethod::Ping,
            params: None,
            req_id: Some(req_id),
        };

        self.send_request(&request).await
    }

    async fn send_request(&self, request: &KrakenWsRequest) -> Result<(), KrakenWsError> {
        let message =
            serde_json::to_string(request).map_err(|e| KrakenWsError::JsonError(e.to_string()))?;

        if let Some(ws_client) = &self.ws_client {
            tracing::trace!("Sending message: {message}");
            ws_client
                .send_text(message, None)
                .await
                .map_err(|e| KrakenWsError::ConnectionError(e.to_string()))?;
        } else {
            return Err(KrakenWsError::Disconnected(
                "WebSocket not connected".into(),
            ));
        }

        Ok(())
    }

    pub fn is_connected(&self) -> bool {
        self.ws_client.is_some()
    }

    pub fn get_subscriptions(&self) -> Vec<String> {
        self.subscriptions
            .iter()
            .map(|entry| entry.key().clone())
            .collect()
    }

    pub fn stream(&mut self) -> impl futures_util::Stream<Item = String> + use<> {
        let mut rx = self
            .raw_rx
            .take()
            .expect("Stream receiver already taken or client not connected");

        async_stream::stream! {
            while let Some(msg) = rx.recv().await {
                match msg {
                    Message::Text(text) => yield text.to_string(),
                    Message::Binary(data) => {
                        if let Ok(text) = String::from_utf8(data.to_vec()) {
                            yield text;
                        }
                    }
                    Message::Ping(_) | Message::Pong(_) => {
                        tracing::trace!("Received ping/pong");
                    }
                    Message::Close(_) => {
                        tracing::info!("WebSocket connection closed");
                        break;
                    }
                    Message::Frame(_) => {
                        tracing::trace!("Received raw frame");
                    }
                }
            }
        }
    }
}

////////////////////////////////////////////////////////////////////////////////
// Tests
////////////////////////////////////////////////////////////////////////////////

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_client_creation() {
        let config = KrakenDataClientConfig::default();
        let token = CancellationToken::new();
        let client = KrakenWebSocketClient::new(config, token);
        assert!(!client.is_connected());
    }
}
