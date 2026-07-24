# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
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

import os
from typing import Any

import httpx
from py_clob_client_v2.http_helpers import helpers as clob_http_helpers


GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
POLYMARKET_API_BLOCKED_COUNTRIES = frozenset({
    "AU",
    "BE",
    "BY",
    "BI",
    "CF",
    "CD",
    "CU",
    "DE",
    "ET",
    "FR",
    "GB",
    "IR",
    "IQ",
    "IT",
    "KP",
    "LB",
    "LY",
    "MM",
    "NI",
    "NL",
    "RU",
    "SO",
    "SS",
    "SD",
    "SY",
    "UM",
    "US",
    "VE",
    "YE",
    "ZW",
})
POLYMARKET_API_CLOSE_ONLY_COUNTRIES = frozenset({"PL", "SG", "TH", "TW"})
POLYMARKET_FRONTEND_ONLY_COUNTRIES = frozenset({"JP"})
POLYMARKET_API_BLOCKED_REGIONS = frozenset({
    ("CA", "ON"),
    ("UA", "43"),
    ("UA", "14"),
    ("UA", "09"),
})

_UNCONFIGURED = object()
_configured_proxy_url: str | None | object = _UNCONFIGURED
CLOB_HTTP_CONNECT_RETRIES = 1


def _resolve_env_proxy_url() -> str | None:
    # CLOB REST 只有 https 端点,按标准语义取 https_proxy,其次 all_proxy。
    for var in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        value = os.environ.get(var)
        if value:
            return value
    return None


def configure_clob_http_transport(proxy_url: str | None) -> None:
    """
    Configure py_clob_client_v2's shared HTTP client.

    The v2 SDK owns a module-level ``httpx.Client`` and otherwise reads process
    proxy environment variables. When ``venues.polymarket.proxy_url`` is set, the
    project config must win so CLOB REST and CLOB WS use the same explicit route.

    ``proxy_url=None`` 时必须显式解析代理环境变量再传给 transport:httpx 的
    ``HTTPTransport(trust_env=True)`` 只读 SSL 证书环境变量、不读代理变量(代理
    环境变量在 Client 层 mounts 生效,而显式 ``transport=`` 会绕过 mounts),
    否则 swap 会把 CLOB REST 从环境代理改成直连。
    """
    global _configured_proxy_url

    if proxy_url == _configured_proxy_url:
        return

    old_client = clob_http_helpers._http_client
    effective_proxy_url = proxy_url if proxy_url is not None else _resolve_env_proxy_url()
    transport = httpx.HTTPTransport(
        http2=True,
        proxy=effective_proxy_url,
        trust_env=proxy_url is None,
        retries=CLOB_HTTP_CONNECT_RETRIES,
    )
    clob_http_helpers._http_client = httpx.Client(
        transport=transport,
        trust_env=False,
    )
    old_client.close()
    _configured_proxy_url = proxy_url


def check_polymarket_geoblock(proxy_url: str | None, timeout: float = 10.0) -> dict[str, Any]:
    """
    Return Polymarket geoblock status for the same HTTP route used by CLOB REST.

    Raises
    ------
    RuntimeError
        If the endpoint cannot be checked or the route is blocked for trading.
    """
    try:
        with httpx.Client(
            proxy=proxy_url,
            trust_env=proxy_url is None,
            timeout=timeout,
        ) as client:
            response = client.get(GEOBLOCK_URL)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        raise RuntimeError(f"Polymarket geoblock preflight failed: {e}") from e

    if not isinstance(data, dict):
        raise RuntimeError("Polymarket geoblock preflight returned a non-object response")

    country = str(data.get("country") or "unknown")
    region = str(data.get("region") or "unknown")

    if (
        country in POLYMARKET_API_BLOCKED_COUNTRIES
        or country in POLYMARKET_API_CLOSE_ONLY_COUNTRIES
        or (country, region) in POLYMARKET_API_BLOCKED_REGIONS
    ):
        raise RuntimeError(
            f"Polymarket trading is geoblocked for the configured HTTP route "
            f"(country={country}, region={region})",
        )

    return data
