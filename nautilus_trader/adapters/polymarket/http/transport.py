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


def configure_clob_http_transport(proxy_url: str | None) -> None:
    """
    Configure py_clob_client_v2's shared HTTP client.

    #276 路由政策:显式 ``venues.polymarket.proxy_url`` 或直连,不读代理环境
    变量(v2 SDK 原生 client 的 ``trust_env=True`` 会读,故必须 swap 收口),
    保证 CLOB REST 与 CLOB WS 同路由。
    """
    global _configured_proxy_url

    if proxy_url == _configured_proxy_url:
        return

    old_client = clob_http_helpers._http_client
    transport = httpx.HTTPTransport(
        http2=True,
        proxy=proxy_url,
        trust_env=False,
        retries=CLOB_HTTP_CONNECT_RETRIES,
    )
    clob_http_helpers._http_client = httpx.Client(
        transport=transport,
        trust_env=False,
    )
    old_client.close()
    _configured_proxy_url = proxy_url


_relayer_configured_proxy_url: str | None | object = _UNCONFIGURED


def configure_relayer_http_transport(proxy_url: str | None) -> None:
    """
    Configure py_builder_relayer_client's HTTP transport.

    #276 路由政策同 CLOB:显式 proxy 或直连。relayer SDK 的 helpers 直接调
    ``requests.request``(requests 默认 ``trust_env=True`` 读代理环境变量),
    故换成显式路由的 ``Session``;helpers 仅引用 ``requests.{request,
    JSONDecodeError, RequestException}``,shim 只需覆盖这三个名字。
    """
    global _relayer_configured_proxy_url

    if proxy_url == _relayer_configured_proxy_url:
        return

    import requests
    from py_builder_relayer_client.http_helpers import helpers as relayer_http_helpers

    session = requests.Session()
    session.trust_env = False
    if proxy_url is not None:
        session.proxies = {"http": proxy_url, "https": proxy_url}

    class _RequestsShim:
        JSONDecodeError = requests.JSONDecodeError
        RequestException = requests.RequestException
        request = staticmethod(session.request)

    relayer_http_helpers.requests = _RequestsShim
    _relayer_configured_proxy_url = proxy_url


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
            trust_env=False,
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
