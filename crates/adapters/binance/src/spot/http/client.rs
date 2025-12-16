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

//! Binance Spot HTTP client with SBE encoding.
//!
//! This client communicates with Binance Spot REST API using SBE (Simple Binary
//! Encoding) for all request/response payloads, providing microsecond timestamp
//! precision and reduced latency compared to JSON.
//!
//! ## SBE Headers
//!
//! All requests include:
//! - `Accept: application/sbe`
//! - `X-MBX-SBE: 3:1` (schema ID:version)

use crate::common::sbe::spot::{SBE_SCHEMA_ID, SBE_SCHEMA_VERSION};

/// Current SBE schema identifier string for HTTP headers.
pub const SBE_SCHEMA_HEADER: &str = "3:1";

/// Binance Spot HTTP client with SBE encoding support.
#[derive(Debug)]
pub struct BinanceSpotHttpClient {
    // TODO: Add fields
    // - http_client: HttpClient
    // - credential: BinanceCredential
    // - base_url: String
    // - rate_limiter: RateLimiter
    _private: (),
}

impl BinanceSpotHttpClient {
    /// Create a new Binance Spot HTTP client.
    #[must_use]
    pub fn new() -> Self {
        Self { _private: () }
    }

    /// Get the SBE schema ID.
    #[must_use]
    pub const fn schema_id() -> u16 {
        SBE_SCHEMA_ID
    }

    /// Get the SBE schema version.
    #[must_use]
    pub const fn schema_version() -> u16 {
        SBE_SCHEMA_VERSION
    }
}

impl Default for BinanceSpotHttpClient {
    fn default() -> Self {
        Self::new()
    }
}
