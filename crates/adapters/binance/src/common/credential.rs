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

//! Binance API credential handling and request signing.

#![allow(unused_assignments)] // Fields are used in methods; false positive on some toolchains

use std::fmt::Debug;

use aws_lc_rs::hmac;
use ustr::Ustr;
use zeroize::ZeroizeOnDrop;

/// Binance API credentials for signing requests.
///
/// Uses HMAC SHA256 with hexadecimal encoding, as required by Binance REST API signing.
#[derive(Clone, ZeroizeOnDrop)]
pub struct Credential {
    #[zeroize(skip)]
    pub api_key: Ustr,
    api_secret: Box<[u8]>,
}

impl Debug for Credential {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct(stringify!(Credential))
            .field("api_key", &self.api_key)
            .field("api_secret", &"<redacted>")
            .finish()
    }
}

impl Credential {
    /// Creates a new [`Credential`] instance.
    #[must_use]
    pub fn new(api_key: String, api_secret: String) -> Self {
        Self {
            api_key: api_key.into(),
            api_secret: api_secret.into_bytes().into_boxed_slice(),
        }
    }

    /// Returns the API key.
    #[must_use]
    pub fn api_key(&self) -> &str {
        self.api_key.as_str()
    }

    /// Signs a message with HMAC SHA256 and returns a lowercase hex digest.
    #[must_use]
    pub fn sign(&self, message: &str) -> String {
        let key = hmac::Key::new(hmac::HMAC_SHA256, &self.api_secret);
        let tag = hmac::sign(&key, message.as_bytes());
        hex::encode(tag.as_ref())
    }
}

#[cfg(test)]
mod tests {
    use rstest::rstest;

    use super::*;

    // Official Binance test vectors from:
    // https://github.com/binance/binance-signature-examples
    const BINANCE_TEST_SECRET: &str =
        "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j";

    #[rstest]
    fn test_sign_matches_binance_test_vector_simple() {
        let cred = Credential::new("test_key".to_string(), BINANCE_TEST_SECRET.to_string());
        let message = "timestamp=1578963600000";
        let expected = "d84e6641b1e328e7b418fff030caed655c266299c9355e36ce801ed14631eed4";

        assert_eq!(cred.sign(message), expected);
    }

    #[rstest]
    fn test_sign_matches_binance_test_vector_order() {
        let cred = Credential::new("test_key".to_string(), BINANCE_TEST_SECRET.to_string());
        let message = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559";
        let expected = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71";

        assert_eq!(cred.sign(message), expected);
    }
}
