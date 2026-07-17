"""SharpExch executor 薄封装离线测试。"""

import asyncio

import pytest

from nautilus_trader.adapters.sharpexch.execution import SharpExchLegacyOrder
from nautilus_trader.adapters.sharpexch.executor import SharpExchExecutor


class _FakeContext:
    async def cookies(self):
        return [{"name": "CSRF-TOKEN", "value": "csrf-from-context"}]


class _FakePage:
    def __init__(self, response, *, with_context=False):
        self.response = response
        self.calls = []
        self._context = _FakeContext() if with_context else None

    async def evaluate(self, script, payload):
        self.calls.append({"script": script, "payload": payload})
        return self.response

    def context(self):
        return self._context


def _order():
    return SharpExchLegacyOrder(
        venue="sharpexch",
        market_id="1.259502313",
        selection_id="111",
        handicap=0.0,
        side="BACK",
        price=2.34,
        size=20.0,
    )


def test_place_order_posts_payload_and_parses_offer_id():
    page = _FakePage({
        "1.259502313": {
            "status": "OK",
            "offerIds": {"server-bet-uuid": "OID-1"},
        },
    })
    executor = SharpExchExecutor(fx_getter=lambda: 1.25)

    result = asyncio.run(executor.place_order(_order(), page, timestamp_ms=123456))

    assert result["success"] is True
    assert result["venue_order_id"] == "OID-1"
    assert result["bet_uuid"].startswith("1.259502313_111_0__123456-")
    payload = page.calls[0]["payload"]["payload"]
    assert payload["1.259502313"][0]["size"] == pytest.approx(20.0)
    assert payload["1.259502313"][0]["page"] == "competition"
    assert payload["1.259502313"][0]["persistenceType"] == "LAPSE"
    assert payload["1.259502313"][0]["showLayOddsEnabled"] is False
    assert "/customer/api/placeBets" in page.calls[0]["script"]


def test_place_order_passes_context_csrf_token_when_available():
    page = _FakePage({"1.259502313": {"status": "OK", "offerIds": {}}}, with_context=True)
    executor = SharpExchExecutor()

    asyncio.run(executor.place_order(_order(), page, timestamp_ms=123456))

    assert page.calls[0]["payload"]["csrfToken"] == "csrf-from-context"


def test_place_and_cancel_scripts_do_not_read_document_cookie():
    page = _FakePage({"1.259502313": {"status": "OK", "offerIds": {}}}, with_context=True)
    executor = SharpExchExecutor()

    asyncio.run(executor.place_order(_order(), page, timestamp_ms=123456))
    asyncio.run(executor.cancel_order("1.259502313", "OID-1", page))

    assert "document.cookie" not in page.calls[0]["script"]
    assert "document.cookie" not in page.calls[1]["script"]


def test_place_order_returns_failure_without_page_or_bad_fx():
    executor = SharpExchExecutor(fx_getter=lambda: 1.0)
    assert asyncio.run(executor.place_order(_order(), None))["message"] == "No page available"

    executor = SharpExchExecutor(fx_getter=lambda: 0.0)
    page = _FakePage({})
    result = asyncio.run(executor.place_order(_order(), page, timestamp_ms=123456))
    assert result["success"] is False
    assert result["message"] == "Invalid fx: 0.0"
    assert page.calls == []


def test_cancel_order_posts_payload_and_parses_success():
    page = _FakePage({"status": "OK"})
    executor = SharpExchExecutor()

    result = asyncio.run(executor.cancel_order("1.259502313", "OID-1", page))

    assert result["success"] is True
    assert result["message"] == "Order cancelled via API"
    assert page.calls[0]["payload"]["payload"] == {
        "1.259502313": [{"offerId": "OID-1", "betType": "EXCHANGE"}],
    }
    assert "/customer/api/cancelBets" in page.calls[0]["script"]


def test_cancel_order_passes_context_csrf_token_when_available():
    page = _FakePage({"status": "OK"}, with_context=True)
    executor = SharpExchExecutor()

    asyncio.run(executor.cancel_order("1.259502313", "OID-1", page))

    assert page.calls[0]["payload"]["csrfToken"] == "csrf-from-context"


def test_cancel_order_posts_full_open_bet_when_available():
    page = _FakePage({"1.259502313": {"status": "OK"}})
    executor = SharpExchExecutor()
    bet = {
        "offerId": 22155999,
        "marketId": "1.259502313",
        "selectionId": 8960879,
        "side": "BACK",
        "sizeRemaining": 12,
    }

    result = asyncio.run(executor.cancel_order("1.259502313", "22155999", page, bet=bet))

    assert result["success"] is True
    payload = page.calls[0]["payload"]["payload"]
    assert payload["1.259502313"][0]["offerId"] == 22155999
    assert payload["1.259502313"][0]["selectionId"] == 8960879
    assert payload["1.259502313"][0]["betType"] == "EXCHANGE"


def test_cancel_order_returns_failure_without_page_or_ids():
    executor = SharpExchExecutor()
    assert asyncio.run(executor.cancel_order("M1", "OID-1", None))["message"] == "No page available"

    page = _FakePage({"status": "OK"})
    result = asyncio.run(executor.cancel_order("", "OID-1", page))
    assert result == {"success": False, "message": "Missing market_id or venue_order_id"}
    assert page.calls == []


def test_transport_errors_are_classified_as_unknown_results():
    response = {"error": "Failed to fetch", "_transport_error": True}
    executor = SharpExchExecutor()

    place = asyncio.run(executor.place_order(_order(), _FakePage(response), timestamp_ms=123456))
    cancel = asyncio.run(executor.cancel_order("1.259502313", "OID-1", _FakePage(response)))

    assert place["success"] is False and place["transport_unknown"] is True
    assert cancel["success"] is False and cancel["transport_unknown"] is True
