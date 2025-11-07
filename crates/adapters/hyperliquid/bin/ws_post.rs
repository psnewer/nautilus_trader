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

//! Minimal WS post example: info (l2Book) and an order action with real signing.

use std::env;

use nautilus_hyperliquid::{common::consts::ws_url, websocket::client::HyperliquidWebSocketClient};
use tracing::level_filters::LevelFilter;
use tracing_subscriber::{EnvFilter, fmt};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Structured logging with env-controlled filter (e.g. RUST_LOG=debug)
    let env_filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    fmt()
        .with_env_filter(env_filter)
        .with_max_level(LevelFilter::INFO)
        .init();

    let args: Vec<String> = env::args().collect();
    let testnet = args.get(1).is_some_and(|s| s == "testnet");
    let ws_url = ws_url(testnet);

    tracing::info!(component = "ws_post", %ws_url, ?testnet, "connecting");
    let mut client = HyperliquidWebSocketClient::new(Some(ws_url.to_string()), testnet, None);
    client.connect().await?;
    tracing::info!(component = "ws_post", "websocket connected");

    // Note: This example used the old inner client's special methods (info_l2_book, post_action_raw)
    // which are not available on the new simplified WebSocket client.
    // The new client focuses on subscriptions only. For order placement, use the HTTP client
    // or the execution client which handles both HTTP and WebSocket.

    tracing::warn!(
        component = "ws_post",
        "This example needs updating for new WebSocket API - specialized methods not available"
    );
    tracing::info!(
        component = "ws_post",
        "Use HyperliquidHttpClient for order placement or HyperliquidExecutionClient for full integration"
    );

    Ok(())
}
