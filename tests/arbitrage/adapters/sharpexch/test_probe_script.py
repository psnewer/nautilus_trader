"""SharpExch 独立 probe 的离线安全测试。"""

import asyncio

from scripts import se_fill_probe
from scripts.se_probe import _ProbeSamples
from scripts.se_probe import _competition_counts
from scripts.se_probe import _competition_url
from scripts.se_probe import _sanitize
from nautilus_trader.adapters.sharpexch import web as se_web
from nautilus_trader.adapters.sharpexch.web import se_customer_context
from nautilus_trader.adapters.sharpexch.web import se_dismiss_post_login_popup
from nautilus_trader.adapters.sharpexch.web import se_fetch_json
from nautilus_trader.adapters.sharpexch.web import se_fetch_json_with_browser_context
from nautilus_trader.adapters.sharpexch.web import se_is_customer_url
from nautilus_trader.adapters.sharpexch.web import se_login
from nautilus_trader.adapters.sharpexch.web import SharpExchLoginState


def test_wait_after_login_uses_configured_timeout_for_frame_and_url(monkeypatch):
    calls = []

    async def wait_for_frame(page, *, timeout_ms):
        calls.append(("frame", timeout_ms))
        raise TimeoutError("no iframe")

    class Page:
        async def wait_for_url(self, pattern, *, timeout):
            calls.append((pattern, timeout))

    monkeypatch.setattr(se_web, "se_wait_for_customer_frame", wait_for_frame)

    asyncio.run(se_web._wait_after_login(Page(), timeout_ms=120000))

    assert calls == [("frame", 120000), ("**/customer**", 120000)]


def test_probe_sanitize_redacts_sensitive_nested_keys():
    payload = {
        "username": "alice",
        "nested": {
            "sessionToken": "secret",
            "balance": 12.3,
        },
        "items": [{"authorization": "bearer abc", "market_id": "1-2"}],
    }

    sanitized = _sanitize(payload)

    assert sanitized["username"] == "<redacted>"
    assert sanitized["nested"]["sessionToken"] == "<redacted>"
    assert sanitized["nested"]["balance"] == 12.3
    assert sanitized["items"][0]["authorization"] == "<redacted>"
    assert sanitized["items"][0]["market_id"] == "1-2"


def test_probe_samples_summary_counts_parsed_general_types():
    samples = _ProbeSamples(limit=2)
    samples.record_price({"id": "m1"}, {"market_id": "m1"})
    samples.record_price({"id": "m2"}, None)
    samples.record_price({"id": "m3"}, {"market_id": "m3"})  # limit 后不保存
    samples.record_general({"BALANCE": "{}"}, {"type": "balance", "balance": 1.0})
    samples.record_general({"CURRENT_BETS": "[]"}, {"type": "current_bets", "bets": []})

    summary = samples.summary([{"type": "orders", "url": "wss://x/general"}])

    assert summary["active_websockets"] == [{"type": "orders", "url": "wss://x/general"}]
    assert summary["price_frames"] == 2
    assert summary["parsed_price_frames"] == 1
    assert summary["general_frames"] == 2
    assert summary["balance_frames"] == 1
    assert summary["current_bets_frames"] == 1


def test_competition_url_uses_sport_and_competition_ids():
    event = {"sport_id": "2", "competition_id": "12597512"}

    assert _competition_url("https://portal.sharpxch.com/", event) == (
        "https://portal.sharpxch.com/customer/sport/2/competition/12597512"
    )


def test_is_customer_url_accepts_iframe_root_and_nested_customer_paths():
    assert se_is_customer_url("https://portal.sharpxch.com/customer")
    assert se_is_customer_url("https://portal.sharpxch.com/customer/sport/2")
    assert not se_is_customer_url("https://sharpxch.com/player/")


def test_customer_context_prefers_customer_iframe():
    page = type("Page", (), {})()
    customer = type("Frame", (), {"url": "https://portal.sharpxch.com/customer"})()
    page.frames = [
        type("Frame", (), {"url": "https://sharpxch.com/player/"})(),
        customer,
    ]

    assert se_customer_context(page) is customer


def test_se_fetch_json_passes_timeout_to_browser_context():
    class Context:
        def __init__(self):
            self.arg = None

        async def evaluate(self, script, arg):
            self.arg = arg
            assert "AbortController" in script
            return {"ok": False, "status": 0, "text": "fetch_error:AbortError", "json": None}

    context = Context()

    result = asyncio.run(
        se_fetch_json(
            context,
            "https://portal.sharpxch.com/customer/api/sport/details",
            params={"page": "0"},
            body={"id": "2"},
            timeout_ms=1234,
        ),
    )

    assert result["status"] == 0
    assert context.arg["timeoutMs"] == 1234
    assert context.arg["params"] == {"page": "0"}
    assert context.arg["body"] == {"id": "2"}


def test_se_fetch_json_with_browser_context_uses_context_request_and_csrf():
    class Response:
        ok = True
        status = 200

        async def text(self):
            return '{"ok": true}'

        async def json(self):
            return {"ok": True}

    class Request:
        def __init__(self):
            self.kwargs = None

        async def post(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            return Response()

    class Context:
        def __init__(self):
            self.request = Request()

        async def cookies(self):
            return [{"name": "CSRF-TOKEN", "value": "csrf-token"}]

    context = Context()

    result = asyncio.run(
        se_fetch_json_with_browser_context(
            context,
            "https://portal.sharpxch.com/customer/api/sport/details",
            params={"page": "0"},
            body={"id": "2"},
            timeout_ms=1234,
        ),
    )

    assert result["ok"] is True
    assert context.request.url == "https://portal.sharpxch.com/customer/api/sport/details"
    assert context.request.kwargs["params"] == {"page": "0"}
    assert context.request.kwargs["data"] == {"id": "2"}
    assert context.request.kwargs["headers"]["x-csrf-token"] == "csrf-token"
    assert context.request.kwargs["timeout"] == 1234


def test_login_submits_form_even_when_customer_iframe_exists():
    """customer iframe 只能证明页面存在 app,不能证明 session 有效;表单可见时必须提交。"""

    class FormControl:
        def __init__(self, page, selector):
            self._page = page
            self._selector = selector

        @property
        def first(self):
            return self

        async def fill(self, value, *, timeout):
            self._page.filled.append((self._selector, value, timeout))

        async def click(self, *, timeout):
            self._page.clicked.append((self._selector, timeout))

    class Popup:
        @property
        def first(self):
            return self

        async def wait_for(self, *, state, timeout):
            raise TimeoutError("missing")

    class Mouse:
        async def click(self, x, y):
            raise AssertionError("no popup click expected")

    class Page:
        def __init__(self):
            self.url = "https://sharpxch.com/player/"
            self.frames = [type("Frame", (), {"url": "https://portal.sharpxch.com/customer"})()]
            self.filled = []
            self.clicked = []
            self.mouse = Mouse()

        async def wait_for_selector(self, selector, **kwargs):
            assert selector == 'input[name="username"], input[type="text"]'

        def locator(self, selector):
            if selector == 'div[class*="_postLoginPopup_"]':
                return Popup()
            return FormControl(self, selector)

        async def wait_for_url(self, pattern, *, timeout):
            assert pattern == "**/customer**"
            self.url = "https://portal.sharpxch.com/customer/"

    cfg = type(
        "Config",
        (),
        {"login_url": "https://sharpxch.com/player/", "page_timeout": 120000, "username": "u", "password": "p"},
    )()
    page = Page()

    asyncio.run(se_login(page, cfg))

    assert ('input[name="username"]', "u", 5000) in page.filled
    assert ('input[name="password"]', "p", 5000) in page.filled
    assert page.clicked


def test_login_reuses_customer_iframe_only_when_login_form_missing():
    class Popup:
        @property
        def first(self):
            return self

        async def wait_for(self, *, state, timeout):
            raise TimeoutError("missing")

    class Mouse:
        async def click(self, x, y):
            raise AssertionError("no popup click expected")

    class Page:
        def __init__(self):
            self.url = "https://sharpxch.com/player/"
            self.frames = [type("Frame", (), {"url": "https://portal.sharpxch.com/customer"})()]
            self.mouse = Mouse()
            self.clicked = []

        async def goto(self, url, *, wait_until, timeout):
            self.url = url

        async def wait_for_selector(self, *args, **kwargs):
            raise TimeoutError("missing")

        def locator(self, selector):
            if selector == 'div[class*="_postLoginPopup_"]':
                return Popup()
            raise AssertionError("login controls should not be touched")

    cfg = type(
        "Config",
        (),
        {"login_url": "https://sharpxch.com/player/", "page_timeout": 120000, "username": "u", "password": "p"},
    )()
    page = Page()

    asyncio.run(se_login(page, cfg))

    assert page.clicked == []


def test_login_state_reuses_authenticated_context_without_second_submit():
    class Popup:
        @property
        def first(self):
            return self

        async def wait_for(self, *, state, timeout):
            raise TimeoutError("missing")

    class Mouse:
        async def click(self, x, y):
            raise AssertionError("no popup click expected")

    class Page:
        def __init__(self):
            self.url = "about:blank"
            self.frames = []
            self.mouse = Mouse()
            self.locator_calls = []

        async def goto(self, url, *, wait_until, timeout):
            self.url = url
            self.frames = [type("Frame", (), {"url": "https://portal.sharpxch.com/customer"})()]

        async def wait_for_selector(self, *args, **kwargs):
            raise AssertionError("login form should not be queried after authenticated context")

        def locator(self, selector):
            self.locator_calls.append(selector)
            if selector == 'div[class*="_postLoginPopup_"]':
                return Popup()
            raise AssertionError("login controls should not be touched")

    cfg = type(
        "Config",
        (),
        {"login_url": "https://sharpxch.com/player/", "page_timeout": 120000, "username": "u", "password": "p"},
    )()
    state = SharpExchLoginState(authenticated=True)
    page = Page()

    asyncio.run(se_login(page, cfg, login_state=state))

    assert state.authenticated is True
    assert page.locator_calls == []


def test_login_uses_browser_lock_when_provided():
    class Lock:
        def __init__(self):
            self.events = []

        async def __aenter__(self):
            self.events.append("enter")

        async def __aexit__(self, exc_type, exc, tb):
            self.events.append("exit")

    class Popup:
        @property
        def first(self):
            return self

        async def wait_for(self, *, state, timeout):
            raise TimeoutError("missing")

    class Page:
        def __init__(self):
            self.url = "https://portal.sharpxch.com/customer/"
            self.frames = []
            self.mouse = type("Mouse", (), {"click": lambda *args, **kwargs: None})()

        async def wait_for_selector(self, *args, **kwargs):
            raise TimeoutError("missing")

        def locator(self, selector):
            return Popup()

    cfg = type(
        "Config",
        (),
        {"login_url": "https://sharpxch.com/player/", "page_timeout": 120000, "username": "u", "password": "p"},
    )()
    lock = Lock()

    asyncio.run(se_login(Page(), cfg, browser_lock=lock))

    assert lock.events == ["enter", "exit"]


def test_competition_counts_sorts_by_count_desc_then_name():
    events = [
        {"competition": "B"},
        {"competition": "A"},
        {"competition": "B"},
        {"competition": "C"},
        {"competition": "A"},
        {"competition": "A"},
    ]

    assert _competition_counts(events) == [("A", 3), ("B", 2), ("C", 1)]


def test_dismiss_post_login_popup_clicks_main_page_when_popup_visible():
    class Popup:
        async def wait_for(self, *, state, timeout):
            assert state == "visible"
            assert 0 < timeout <= 1000

    class Locator:
        @property
        def first(self):
            return Popup()

    class Mouse:
        def __init__(self):
            self.clicks = []

        async def click(self, x, y):
            self.clicks.append((x, y))

    class Page:
        def __init__(self):
            self.mouse = Mouse()

        def locator(self, selector):
            assert selector == 'div[class*="_postLoginPopup_"]'
            return Locator()

    page = Page()

    assert asyncio.run(se_dismiss_post_login_popup(page)) is True
    assert page.mouse.clicks == [(24, 160)]


def test_dismiss_post_login_popup_timeout_continues_without_click():
    class Popup:
        async def wait_for(self, *, state, timeout):
            raise TimeoutError("missing")

    class Locator:
        @property
        def first(self):
            return Popup()

    class Mouse:
        def __init__(self):
            self.clicks = []

        async def click(self, x, y):
            self.clicks.append((x, y))

    class Page:
        def __init__(self):
            self.mouse = Mouse()

        def locator(self, selector):
            return Locator()

    page = Page()

    assert asyncio.run(se_dismiss_post_login_popup(page, timeout_ms=300)) is False
    assert page.mouse.clicks == []


def test_se_fill_probe_defaults_to_min_stake_marketable_back():
    args = se_fill_probe._build_parser().parse_args(["--config", "arb_config.json"])

    assert args.confirm is False
    assert args.size == 12.0
    assert args.odds == 1.01
    assert args.discovery_timeout == 120.0


def test_se_fill_probe_records_accepted_venue_order_mapping():
    class Cache:
        def __init__(self):
            self.calls = []

        def add_venue_order_id(self, client_order_id, venue_order_id):
            self.calls.append((client_order_id, venue_order_id))

    cache = Cache()

    se_fill_probe._record_accepted_probe_cache(cache, "COID-1", "VOID-1")

    assert cache.calls == [("COID-1", "VOID-1")]


def test_se_fill_probe_can_override_user_data_dir():
    cfg = se_fill_probe.SharpExchExecClientConfig(
        username="u",
        password="p",
        user_data_dir=None,
    )

    out = se_fill_probe._exec_config_with_user_data_dir(cfg, "/tmp/se-profile")

    assert out.user_data_dir == "/tmp/se-profile"
    assert out.username == "u"
    assert out.password == "p"
