import asyncio
from types import SimpleNamespace

import msgspec
import pytest

from nautilus_trader.adapters.polymarket.common.gamma_markets import fetch_gamma_events_keyset


class _HttpClient:
    def __init__(self, pages):
        self._pages = pages
        self.calls = []

    async def get(self, url, *, params, timeout_secs):
        self.calls.append((url, dict(params), timeout_secs))
        cursor = params.get("after_cursor")
        payload = self._pages[cursor]
        return SimpleNamespace(status=200, body=msgspec.json.encode(payload))


def test_fetch_gamma_events_keyset_collects_all_pages():
    client = _HttpClient(
        {
            None: {"events": [{"id": "1"}, {"id": "2"}], "next_cursor": "cursor-1"},
            "cursor-1": {"events": [{"id": "3"}], "next_cursor": ""},
        },
    )

    events = asyncio.run(
        fetch_gamma_events_keyset(
            client,
            {
                "series_id": "10365",
                "closed": "false",
                "active": "true",
                "limit": 500,
                "offset": 100,
            },
        ),
    )

    assert events == [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    assert len(client.calls) == 2
    first_url, first_params, first_timeout = client.calls[0]
    assert first_url.endswith("/events/keyset")
    assert first_params == {
        "series_id": "10365",
        "closed": "false",
        "active": "true",
        "limit": "20",
    }
    assert first_timeout == 30
    assert client.calls[1][1]["after_cursor"] == "cursor-1"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"next_cursor": ""},
        {"events": [1], "next_cursor": ""},
        {"events": [], "next_cursor": 123},
    ],
)
def test_fetch_gamma_events_keyset_rejects_invalid_page(payload):
    client = _HttpClient({None: payload})

    with pytest.raises(RuntimeError):
        asyncio.run(
            fetch_gamma_events_keyset(
                client,
                {"series_id": "10365"},
            ),
        )


def test_fetch_gamma_events_keyset_rejects_repeated_cursor():
    client = _HttpClient(
        {
            None: {"events": [{"id": "1"}], "next_cursor": "same"},
            "same": {"events": [{"id": "2"}], "next_cursor": "same"},
        },
    )

    with pytest.raises(RuntimeError, match="repeated next_cursor"):
        asyncio.run(
            fetch_gamma_events_keyset(
                client,
                {"series_id": "10365"},
            ),
        )
