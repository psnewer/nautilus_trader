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

//! HTTP client for the Kraken REST API v2.

use std::{
    collections::HashMap,
    fmt::{Debug, Formatter},
    num::NonZeroU32,
    sync::{
        Arc, LazyLock,
        atomic::{AtomicBool, Ordering},
    },
};

use dashmap::DashMap;
use nautilus_core::consts::NAUTILUS_USER_AGENT;
use nautilus_model::instruments::{Instrument, InstrumentAny};
use nautilus_network::{
    http::HttpClient,
    ratelimiter::quota::Quota,
    retry::{RetryConfig, RetryManager},
};
use reqwest::{Method, header::USER_AGENT};
use serde::de::DeserializeOwned;
use tokio_util::sync::CancellationToken;
use ustr::Ustr;

use super::{error::KrakenHttpError, models::*};
use crate::common::{
    credential::KrakenCredential,
    enums::{KrakenEnvironment, KrakenProductType},
    urls::get_http_base_url,
};

pub static KRAKEN_REST_QUOTA: LazyLock<Quota> = LazyLock::new(|| {
    Quota::per_second(NonZeroU32::new(5).expect("Should be a valid non-zero u32"))
});

const KRAKEN_GLOBAL_RATE_KEY: &str = "kraken:global";

#[derive(Debug, Clone, serde::Deserialize)]
pub struct KrakenResponse<T> {
    pub error: Vec<String>,
    pub result: Option<T>,
}

pub struct KrakenRawHttpClient {
    base_url: String,
    client: HttpClient,
    credential: Option<KrakenCredential>,
    retry_manager: RetryManager<KrakenHttpError>,
    cancellation_token: CancellationToken,
}

impl Default for KrakenRawHttpClient {
    fn default() -> Self {
        Self::new(None, Some(60), None, None, None, None)
            .expect("Failed to create default KrakenRawHttpClient")
    }
}

impl Debug for KrakenRawHttpClient {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("KrakenRawHttpClient")
            .field("base_url", &self.base_url)
            .field("has_credentials", &self.credential.is_some())
            .finish()
    }
}

impl KrakenRawHttpClient {
    pub fn cancel_all_requests(&self) {
        self.cancellation_token.cancel();
    }

    pub fn cancellation_token(&self) -> &CancellationToken {
        &self.cancellation_token
    }

    #[allow(clippy::too_many_arguments)]
    pub fn new(
        base_url: Option<String>,
        timeout_secs: Option<u64>,
        max_retries: Option<u32>,
        retry_delay_ms: Option<u64>,
        retry_delay_max_ms: Option<u64>,
        proxy_url: Option<String>,
    ) -> anyhow::Result<Self> {
        let retry_config = RetryConfig {
            max_retries: max_retries.unwrap_or(3),
            initial_delay_ms: retry_delay_ms.unwrap_or(1000),
            max_delay_ms: retry_delay_max_ms.unwrap_or(10_000),
            backoff_factor: 2.0,
            jitter_ms: 1000,
            operation_timeout_ms: Some(60_000),
            immediate_first: false,
            max_elapsed_ms: Some(180_000),
        };

        let retry_manager = RetryManager::new(retry_config);

        Ok(Self {
            base_url: base_url.unwrap_or_else(|| {
                get_http_base_url(KrakenProductType::Spot, KrakenEnvironment::Mainnet).to_string()
            }),
            client: HttpClient::new(
                Self::default_headers(),
                vec![],
                Self::rate_limiter_quotas(),
                Some(*KRAKEN_REST_QUOTA),
                timeout_secs,
                proxy_url,
            )
            .map_err(|e| anyhow::anyhow!("Failed to create HTTP client: {e}"))?,
            credential: None,
            retry_manager,
            cancellation_token: CancellationToken::new(),
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub fn with_credentials(
        api_key: String,
        api_secret: String,
        base_url: Option<String>,
        timeout_secs: Option<u64>,
        max_retries: Option<u32>,
        retry_delay_ms: Option<u64>,
        retry_delay_max_ms: Option<u64>,
        proxy_url: Option<String>,
    ) -> anyhow::Result<Self> {
        let retry_config = RetryConfig {
            max_retries: max_retries.unwrap_or(3),
            initial_delay_ms: retry_delay_ms.unwrap_or(1000),
            max_delay_ms: retry_delay_max_ms.unwrap_or(10_000),
            backoff_factor: 2.0,
            jitter_ms: 1000,
            operation_timeout_ms: Some(60_000),
            immediate_first: false,
            max_elapsed_ms: Some(180_000),
        };

        let retry_manager = RetryManager::new(retry_config);

        Ok(Self {
            base_url: base_url.unwrap_or_else(|| {
                get_http_base_url(KrakenProductType::Spot, KrakenEnvironment::Mainnet).to_string()
            }),
            client: HttpClient::new(
                Self::default_headers(),
                vec![],
                Self::rate_limiter_quotas(),
                Some(*KRAKEN_REST_QUOTA),
                timeout_secs,
                proxy_url,
            )
            .map_err(|e| anyhow::anyhow!("Failed to create HTTP client: {e}"))?,
            credential: Some(KrakenCredential::new(api_key, api_secret)),
            retry_manager,
            cancellation_token: CancellationToken::new(),
        })
    }

    fn default_headers() -> HashMap<String, String> {
        HashMap::from([(USER_AGENT.to_string(), NAUTILUS_USER_AGENT.to_string())])
    }

    fn rate_limiter_quotas() -> Vec<(String, Quota)> {
        vec![(KRAKEN_GLOBAL_RATE_KEY.to_string(), *KRAKEN_REST_QUOTA)]
    }

    fn rate_limit_keys(endpoint: &str) -> Vec<String> {
        let normalized = endpoint.split('?').next().unwrap_or(endpoint);
        let route = format!("kraken:{normalized}");
        vec![KRAKEN_GLOBAL_RATE_KEY.to_string(), route]
    }

    fn sign_request(
        &self,
        path: &str,
        nonce: u64,
        params: &HashMap<String, String>,
    ) -> anyhow::Result<HashMap<String, String>> {
        let credential = self
            .credential
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("Missing credentials"))?;

        let signature = credential.sign_request(path, nonce, params)?;

        let mut headers = HashMap::new();
        headers.insert("API-Key".to_string(), credential.api_key().to_string());
        headers.insert("API-Sign".to_string(), signature);

        Ok(headers)
    }

    async fn send_request<T: DeserializeOwned>(
        &self,
        method: Method,
        endpoint: &str,
        body: Option<Vec<u8>>,
        authenticate: bool,
    ) -> anyhow::Result<KrakenResponse<T>, KrakenHttpError> {
        let endpoint = endpoint.to_string();
        let url = format!("{}{endpoint}", self.base_url);
        let method_clone = method.clone();
        let body_clone = body.clone();

        let operation = || {
            let url = url.clone();
            let method = method_clone.clone();
            let body = body_clone.clone();
            let endpoint = endpoint.clone();

            async move {
                let mut headers = Self::default_headers();

                if authenticate {
                    let nonce = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .expect("Time went backwards")
                        .as_millis() as u64;

                    let params = if let Some(ref body_bytes) = body {
                        let body_str = std::str::from_utf8(body_bytes).map_err(|e| {
                            KrakenHttpError::ParseError(format!(
                                "Invalid UTF-8 in request body: {e}"
                            ))
                        })?;
                        serde_urlencoded::from_str(body_str).map_err(|e| {
                            KrakenHttpError::ParseError(format!(
                                "Failed to parse request params: {e}"
                            ))
                        })?
                    } else {
                        HashMap::new()
                    };

                    let auth_headers = self
                        .sign_request(&endpoint, nonce, &params)
                        .map_err(|e| KrakenHttpError::NetworkError(e.to_string()))?;
                    headers.extend(auth_headers);
                }

                if method == Method::POST {
                    headers.insert(
                        "Content-Type".to_string(),
                        "application/x-www-form-urlencoded".to_string(),
                    );
                }

                let rate_limit_keys = Self::rate_limit_keys(&endpoint);

                let response = self
                    .client
                    .request(
                        method,
                        url,
                        None,
                        Some(headers),
                        body,
                        None,
                        Some(rate_limit_keys),
                    )
                    .await
                    .map_err(|e| KrakenHttpError::NetworkError(e.to_string()))?;

                if response.status.as_u16() >= 400 {
                    let body = String::from_utf8_lossy(&response.body).to_string();
                    return Err(KrakenHttpError::NetworkError(format!(
                        "HTTP error {}: {body}",
                        response.status.as_u16()
                    )));
                }

                let response_text = String::from_utf8(response.body.to_vec()).map_err(|e| {
                    KrakenHttpError::ParseError(format!("Failed to parse response as UTF-8: {e}"))
                })?;

                let kraken_response: KrakenResponse<T> = serde_json::from_str(&response_text)
                    .map_err(|e| {
                        KrakenHttpError::ParseError(format!("Failed to deserialize response: {e}"))
                    })?;

                if !kraken_response.error.is_empty() {
                    return Err(KrakenHttpError::ApiError(kraken_response.error.clone()));
                }

                Ok(kraken_response)
            }
        };

        let should_retry =
            |error: &KrakenHttpError| -> bool { matches!(error, KrakenHttpError::NetworkError(_)) };

        let create_error = |msg: String| -> KrakenHttpError { KrakenHttpError::NetworkError(msg) };

        self.retry_manager
            .execute_with_retry_with_cancel(
                &endpoint,
                operation,
                should_retry,
                create_error,
                &self.cancellation_token,
            )
            .await
    }

    pub async fn get_server_time(&self) -> anyhow::Result<ServerTime, KrakenHttpError> {
        let response: KrakenResponse<ServerTime> = self
            .send_request(Method::GET, "/0/public/Time", None, false)
            .await?;

        response.result.ok_or_else(|| {
            KrakenHttpError::ParseError("Missing result in server time response".to_string())
        })
    }

    pub async fn get_system_status(&self) -> anyhow::Result<SystemStatus, KrakenHttpError> {
        let response: KrakenResponse<SystemStatus> = self
            .send_request(Method::GET, "/0/public/SystemStatus", None, false)
            .await?;

        response.result.ok_or_else(|| {
            KrakenHttpError::ParseError("Missing result in system status response".to_string())
        })
    }

    pub async fn get_asset_pairs(
        &self,
        pairs: Option<Vec<String>>,
    ) -> anyhow::Result<AssetPairsResponse, KrakenHttpError> {
        let endpoint = if let Some(pairs) = pairs {
            format!("/0/public/AssetPairs?pair={}", pairs.join(","))
        } else {
            "/0/public/AssetPairs".to_string()
        };

        let response: KrakenResponse<AssetPairsResponse> = self
            .send_request(Method::GET, &endpoint, None, false)
            .await?;

        response.result.ok_or_else(|| {
            KrakenHttpError::ParseError("Missing result in asset pairs response".to_string())
        })
    }

    pub async fn get_ticker(
        &self,
        pairs: Vec<String>,
    ) -> anyhow::Result<TickerResponse, KrakenHttpError> {
        let endpoint = format!("/0/public/Ticker?pair={}", pairs.join(","));

        let response: KrakenResponse<TickerResponse> = self
            .send_request(Method::GET, &endpoint, None, false)
            .await?;

        response.result.ok_or_else(|| {
            KrakenHttpError::ParseError("Missing result in ticker response".to_string())
        })
    }

    pub async fn get_ohlc(
        &self,
        pair: &str,
        interval: Option<u32>,
        since: Option<i64>,
    ) -> anyhow::Result<OhlcResponse, KrakenHttpError> {
        let mut endpoint = format!("/0/public/OHLC?pair={pair}");

        if let Some(interval) = interval {
            endpoint.push_str(&format!("&interval={interval}"));
        }
        if let Some(since) = since {
            endpoint.push_str(&format!("&since={since}"));
        }

        let response: KrakenResponse<OhlcResponse> = self
            .send_request(Method::GET, &endpoint, None, false)
            .await?;

        response.result.ok_or_else(|| {
            KrakenHttpError::ParseError("Missing result in OHLC response".to_string())
        })
    }

    pub async fn get_order_book(
        &self,
        pair: &str,
        count: Option<u32>,
    ) -> anyhow::Result<OrderBookResponse, KrakenHttpError> {
        let mut endpoint = format!("/0/public/Depth?pair={pair}");

        if let Some(count) = count {
            endpoint.push_str(&format!("&count={count}"));
        }

        let response: KrakenResponse<OrderBookResponse> = self
            .send_request(Method::GET, &endpoint, None, false)
            .await?;

        response.result.ok_or_else(|| {
            KrakenHttpError::ParseError("Missing result in order book response".to_string())
        })
    }

    pub async fn get_trades(
        &self,
        pair: &str,
        since: Option<String>,
    ) -> anyhow::Result<TradesResponse, KrakenHttpError> {
        let mut endpoint = format!("/0/public/Trades?pair={pair}");

        if let Some(since) = since {
            endpoint.push_str(&format!("&since={since}"));
        }

        let response: KrakenResponse<TradesResponse> = self
            .send_request(Method::GET, &endpoint, None, false)
            .await?;

        response.result.ok_or_else(|| {
            KrakenHttpError::ParseError("Missing result in trades response".to_string())
        })
    }
}

pub struct KrakenHttpClient {
    pub(crate) inner: Arc<KrakenRawHttpClient>,
    pub(crate) instruments_cache: Arc<DashMap<Ustr, InstrumentAny>>,
    cache_initialized: Arc<AtomicBool>,
}

impl Clone for KrakenHttpClient {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
            instruments_cache: self.instruments_cache.clone(),
            cache_initialized: self.cache_initialized.clone(),
        }
    }
}

impl Default for KrakenHttpClient {
    fn default() -> Self {
        Self::new(None, Some(60), None, None, None, None)
            .expect("Failed to create default KrakenHttpClient")
    }
}

impl Debug for KrakenHttpClient {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("KrakenHttpClient")
            .field("inner", &self.inner)
            .finish()
    }
}

impl KrakenHttpClient {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        base_url: Option<String>,
        timeout_secs: Option<u64>,
        max_retries: Option<u32>,
        retry_delay_ms: Option<u64>,
        retry_delay_max_ms: Option<u64>,
        proxy_url: Option<String>,
    ) -> anyhow::Result<Self> {
        Ok(Self {
            inner: Arc::new(KrakenRawHttpClient::new(
                base_url,
                timeout_secs,
                max_retries,
                retry_delay_ms,
                retry_delay_max_ms,
                proxy_url,
            )?),
            instruments_cache: Arc::new(DashMap::new()),
            cache_initialized: Arc::new(AtomicBool::new(false)),
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub fn with_credentials(
        api_key: String,
        api_secret: String,
        base_url: Option<String>,
        timeout_secs: Option<u64>,
        max_retries: Option<u32>,
        retry_delay_ms: Option<u64>,
        retry_delay_max_ms: Option<u64>,
        proxy_url: Option<String>,
    ) -> anyhow::Result<Self> {
        Ok(Self {
            inner: Arc::new(KrakenRawHttpClient::with_credentials(
                api_key,
                api_secret,
                base_url,
                timeout_secs,
                max_retries,
                retry_delay_ms,
                retry_delay_max_ms,
                proxy_url,
            )?),
            instruments_cache: Arc::new(DashMap::new()),
            cache_initialized: Arc::new(AtomicBool::new(false)),
        })
    }

    pub fn cancel_all_requests(&self) {
        self.inner.cancel_all_requests();
    }

    pub fn cancellation_token(&self) -> &CancellationToken {
        self.inner.cancellation_token()
    }

    pub fn cache_instrument(&self, instrument: InstrumentAny) {
        self.instruments_cache
            .insert(instrument.symbol().inner(), instrument);
        self.cache_initialized.store(true, Ordering::Release);
    }

    pub fn cache_instruments(&self, instruments: Vec<InstrumentAny>) {
        for instrument in instruments {
            self.instruments_cache
                .insert(instrument.symbol().inner(), instrument);
        }
        self.cache_initialized.store(true, Ordering::Release);
    }

    pub fn get_instrument(&self, symbol: &Ustr) -> Option<InstrumentAny> {
        self.instruments_cache
            .get(symbol)
            .map(|entry| entry.value().clone())
    }
}

////////////////////////////////////////////////////////////////////////////////
// Tests
////////////////////////////////////////////////////////////////////////////////

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_raw_client_creation() {
        let client = KrakenRawHttpClient::default();
        assert!(client.credential.is_none());
    }

    #[test]
    fn test_raw_client_with_credentials() {
        let client = KrakenRawHttpClient::with_credentials(
            "test_key".to_string(),
            "test_secret".to_string(),
            None,
            None,
            None,
            None,
            None,
            None,
        )
        .unwrap();
        assert!(client.credential.is_some());
    }

    #[test]
    fn test_client_creation() {
        let client = KrakenHttpClient::default();
        assert!(client.instruments_cache.is_empty());
    }

    #[test]
    fn test_client_with_credentials() {
        let client = KrakenHttpClient::with_credentials(
            "test_key".to_string(),
            "test_secret".to_string(),
            None,
            None,
            None,
            None,
            None,
            None,
        )
        .unwrap();
        assert!(client.instruments_cache.is_empty());
    }
}
