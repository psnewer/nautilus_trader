"""SharpExch executor 薄封装。

本模块不管理浏览器生命周期、不登录、不接 NT runtime;只封装给定 page 时的
place/cancel request 边界,方便后续 ExecutionClient 复用。
"""

from __future__ import annotations

from collections.abc import Callable

from nautilus_trader.adapters.sharpexch.execution import SharpExchLegacyOrder
from nautilus_trader.adapters.sharpexch.execution import parse_cancel_bets_response
from nautilus_trader.adapters.sharpexch.execution import parse_place_bets_response
from nautilus_trader.adapters.sharpexch.execution import se_order_to_cancel_bets_payload
from nautilus_trader.adapters.sharpexch.execution import se_order_to_place_bets_payload
from nautilus_trader.adapters.sharpexch.web import se_customer_context


_PLACE_BETS_JS = """async (arg) => {
    try {
        const payload = arg && arg.payload ? arg.payload : arg;
        let csrfToken = arg && arg.csrfToken ? arg.csrfToken : '';
        if (!csrfToken) {
            return { error: 'CSRF token not found in browser context cookies' };
        }
        const bodyStr = JSON.stringify(payload);
        const response = await fetch('/customer/api/placeBets', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/plain, */*',
                'x-csrf-token': csrfToken,
                'x-device': 'DESKTOP',
                'Origin': window.location.origin,
                'Referer': window.location.href,
            },
            body: bodyStr,
            credentials: 'include',
        });
        const text = await response.text();
        try {
            return JSON.parse(text);
        } catch (e) {
            return {
                error: `json_parse_failed: ${e.message}`,
                status: response.status,
                raw_sample: text.slice(0, 500),
                _transport_error: true,
            };
        }
    } catch (error) {
        return { error: error.message, _transport_error: true };
    }
}"""

_CANCEL_BETS_JS = """async (arg) => {
    try {
        const payload = arg && arg.payload ? arg.payload : arg;
        let csrfToken = arg && arg.csrfToken ? arg.csrfToken : '';
        if (!csrfToken) {
            return { error: 'CSRF token not found in browser context cookies' };
        }
        const bodyStr = JSON.stringify(payload);
        const response = await fetch('/customer/api/cancelBets', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/plain, */*',
                'x-csrf-token': csrfToken,
                'x-device': 'DESKTOP',
                'Origin': window.location.origin,
                'Referer': window.location.href,
            },
            body: bodyStr,
            credentials: 'include',
        });
        const text = await response.text();
        try {
            return JSON.parse(text);
        } catch (e) {
            return {
                error: `json_parse_failed: ${e.message}`,
                status: response.status,
                raw_sample: text.slice(0, 500),
                _transport_error: true,
            };
        }
    } catch (error) {
        return { error: error.message, _transport_error: true };
    }
}"""


class SharpExchExecutor:
    """SE page-bound 下单/撤单薄封装。

    `page` 由 BrowserManager/ExecutionClient 传入;本类不持有真实账户状态。
    """

    def __init__(
        self,
        *,
        fx_getter: Callable[[], float] | None = None,
        persistence_type: str = "LAPSE",
    ) -> None:
        self._fx_getter = fx_getter or (lambda: 1.0)
        self._persistence_type = persistence_type

    async def place_order(
        self,
        order: SharpExchLegacyOrder,
        page,
        *,
        timestamp_ms: int | None = None,
        market: bool = False,
    ) -> dict:
        if page is None:
            return {
                "success": False,
                "venue_order_id": None,
                "message": "No page available",
            }
        try:
            payload, bet_uuid = se_order_to_place_bets_payload(
                order,
                self._fx_getter(),
                timestamp_ms=timestamp_ms,
                persistence_type=self._persistence_type,
                market=market,
            )
        except ValueError as exc:
            return {"success": False, "venue_order_id": None, "message": str(exc)}

        response = await se_customer_context(page).evaluate(
            _PLACE_BETS_JS,
            {"payload": payload, "csrfToken": await _csrf_token_from_context(page)},
        )
        result = parse_place_bets_response(response, order.market_id, bet_uuid)
        result["venue_response"] = response
        result["venue_payload"] = payload
        result["bet_uuid"] = bet_uuid
        return result

    async def cancel_order(self, market_id: str, venue_order_id: str, page, *, bet: dict | None = None) -> dict:
        if page is None:
            return {"success": False, "message": "No page available"}
        payload = se_order_to_cancel_bets_payload(market_id, venue_order_id, bet=bet)
        if payload is None:
            return {"success": False, "message": "Missing market_id or venue_order_id"}

        response = await se_customer_context(page).evaluate(
            _CANCEL_BETS_JS,
            {"payload": payload, "csrfToken": await _csrf_token_from_context(page)},
        )
        result = parse_cancel_bets_response(response)
        result["venue_response"] = response
        return result


async def _csrf_token_from_context(page) -> str:
    context_fn = getattr(page, "context", None)
    context = context_fn() if callable(context_fn) else getattr(page, "context", None)
    cookies_fn = getattr(context, "cookies", None)
    if cookies_fn is None:
        return ""
    try:
        cookies = await cookies_fn()
    except TypeError:
        cookies = await cookies_fn([])
    except Exception:  # noqa: BLE001
        return ""
    for cookie in cookies or []:
        if cookie.get("name") == "CSRF-TOKEN":
            return str(cookie.get("value") or "")
    return ""
