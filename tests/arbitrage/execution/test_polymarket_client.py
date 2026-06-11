"""ArbPolymarketExecutionClient —— 离线可测部分(纯映射 + MRO)。

完整集成(真 ClobClient/ws_auth/Data API、_submit_order/_run_health_check 接线)经 /live-test 验。
"""

import inspect
import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from py_clob_client_v2 import ClobClient
from py_clob_client_v2 import PostOrdersArgs as TopLevelPostOrdersArgs
from py_clob_client_v2.clob_types import OrderPayload
from py_clob_client_v2.clob_types import PostOrdersV2Args

from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient
from nautilus_trader.adapters.polymarket.factories import get_polymarket_http_client
from nautilus_trader.adapters.polymarket.http import transport as pm_transport

from nautilus_trader.adapters.polymarket.arb_execution import ArbPolymarketExecutionClient
from nautilus_trader.adapters.polymarket.arb_execution import pm_position_to_settlement
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import VenueOrderId
from src.arbitrage.execution.session import ArbExecutionSessionMixin
from src.arbitrage.settlement.settlement import SettlementPosition


@dataclass
class _PMPos:
    condition_id: str
    size: float
    neg_risk: bool = False
    redeemable: bool = False


class _RetryManager:
    result = True
    message = ""

    async def run(self, _name, _keys, runner, fn, *args):
        return await runner(fn, *args)


class _RetryPool:
    async def acquire(self):
        return _RetryManager()

    async def release(self, _retry_manager):
        return None


class _Clock:
    def timestamp_ns(self):
        return 123


class _Log:
    def info(self, *_args, **_kwargs):
        return None

    def debug(self, *_args, **_kwargs):
        return None

    def error(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None


def _cancel_test_client(response):
    client = SimpleNamespace()
    client._retry_manager_pool = _RetryPool()
    client._clock = _Clock()
    client._log = _Log()
    client._http_client = SimpleNamespace(cancel_order=lambda payload: response)
    client._maintain_active_market = lambda instrument_id: _noop_async()

    captured = {}
    client.generate_order_canceled = lambda **kwargs: captured.update(canceled=kwargs)
    client.generate_order_cancel_rejected = lambda **kwargs: captured.update(rejected=kwargs)

    venue_order_id = VenueOrderId("0x" + "a" * 64)
    order = SimpleNamespace(
        strategy_id="S",
        instrument_id=InstrumentId.from_str("1.POLYMARKET"),
        client_order_id=ClientOrderId("O-1"),
        venue_order_id=venue_order_id,
        is_closed=False,
    )
    client._cache = SimpleNamespace(order=lambda _coid: order)
    client._generate_cancel_event = PolymarketExecutionClient._generate_cancel_event.__get__(client)
    client._generate_cancel_success_event = (
        PolymarketExecutionClient._generate_cancel_success_event.__get__(client)
    )
    client._cancel_order = PolymarketExecutionClient._cancel_order.__get__(client)
    command = SimpleNamespace(
        strategy_id=order.strategy_id,
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        venue_order_id=venue_order_id,
    )
    return client, command, captured, venue_order_id


async def _noop_async():
    return None


def _run(coro):
    return asyncio.run(coro)


def test_mro_mixin_before_upstream():
    # mixin 必须在上游前,才能覆盖 _send_order_event / _submit_order
    mro = ArbPolymarketExecutionClient.__mro__
    assert mro.index(ArbExecutionSessionMixin) < mro.index(PolymarketExecutionClient)


def test_position_to_settlement_maps_fields():
    p = _PMPos(condition_id="0xcond", size=80.0, neg_risk=True, redeemable=True)
    assert pm_position_to_settlement(p) == SettlementPosition(
        condition_id="0xcond", size=80.0, neg_risk=True, redeemable=True,
    )


def test_position_to_settlement_defaults():
    p = _PMPos(condition_id="0xc", size=10.0)
    s = pm_position_to_settlement(p)
    assert s.neg_risk is False and s.redeemable is False


def test_polymarket_execution_uses_py_clob_client_v2_surface():
    """PM live 已要求 CLOB v2;锁 execution/factory 不回退旧 py_clob_client。"""
    assert inspect.signature(get_polymarket_http_client).return_annotation is ClobClient
    assert "py_clob_client_v2 import ClobClient" in inspect.getsource(
        inspect.getmodule(get_polymarket_http_client),
    )

    assert str(inspect.signature(ClobClient.post_order)) == (
        "(self, order, order_type: py_clob_client_v2.clob_types.OrderType = 'GTC', "
        "post_only: bool = False, defer_exec: bool = False)"
    )
    assert str(inspect.signature(PostOrdersV2Args)) == (
        "(order: Any, orderType: py_clob_client_v2.clob_types.OrderType = 'GTC', "
        "deferExec: bool = False) -> None"
    )
    assert str(TopLevelPostOrdersArgs).startswith("typing.Union[")
    assert str(inspect.signature(OrderPayload)) == "(orderID: str) -> None"

    source = inspect.getsource(PolymarketExecutionClient._post_signed_order)
    assert "self._http_client.post_order" in source

    module_source = inspect.getsource(inspect.getmodule(PolymarketExecutionClient))
    assert "PostOrdersV2Args as PostOrdersArgs" in module_source

    batch_source = inspect.getsource(PolymarketExecutionClient._sign_orders_for_batch)
    assert "PostOrdersArgs" in batch_source

    reports_source = inspect.getsource(PolymarketExecutionClient.generate_order_status_reports)
    assert "self._http_client.get_open_orders" in reports_source
    assert "self._http_client.get_orders" not in reports_source

    cancel_source = inspect.getsource(PolymarketExecutionClient._cancel_order)
    assert "self._http_client.cancel_order" in cancel_source
    assert "OrderPayload(orderID=venue_order_id.value)" in cancel_source


def test_polymarket_cancel_order_success_generates_canceled_event():
    venue_order_id = "0x" + "a" * 64
    client, command, captured, expected_venue_order_id = _cancel_test_client({
        "canceled": [venue_order_id],
        "not_canceled": {},
    })

    _run(client._cancel_order(command))

    assert captured["canceled"]["client_order_id"] == ClientOrderId("O-1")
    assert captured["canceled"]["venue_order_id"] == expected_venue_order_id
    assert "rejected" not in captured


def test_polymarket_cancel_success_skips_duplicate_canceled_order():
    venue_order_id = VenueOrderId("0x" + "a" * 64)
    client = SimpleNamespace()
    client._clock = _Clock()
    client._log = _Log()
    client._cache = SimpleNamespace(
        order=lambda _coid: SimpleNamespace(status=OrderStatus.CANCELED),
    )
    captured = {}
    client.generate_order_canceled = lambda **kwargs: captured.update(canceled=kwargs)
    client._generate_cancel_success_event = (
        PolymarketExecutionClient._generate_cancel_success_event.__get__(client)
    )

    client._generate_cancel_success_event(
        strategy_id="S",
        instrument_id=InstrumentId.from_str("1.POLYMARKET"),
        client_order_id=ClientOrderId("O-1"),
        venue_order_id=venue_order_id,
        ts_event=123,
    )

    assert captured == {}


def test_polymarket_cancel_order_reject_generates_cancel_rejected_event():
    venue_order_id = "0x" + "a" * 64
    client, command, captured, expected_venue_order_id = _cancel_test_client({
        "canceled": [],
        "not_canceled": {venue_order_id: "already open on another market"},
    })

    _run(client._cancel_order(command))

    assert captured["rejected"]["client_order_id"] == ClientOrderId("O-1")
    assert captured["rejected"]["venue_order_id"] == expected_venue_order_id
    assert captured["rejected"]["reason"] == "already open on another market"
    assert "canceled" not in captured


def test_polymarket_factory_configures_v2_http_proxy(monkeypatch):
    """PM CLOB REST 必须吃项目 proxy_url,避免 WS/REST 走不同出口。"""
    get_polymarket_http_client.cache_clear()

    created = []

    class _OldClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    old_client = _OldClient()
    monkeypatch.setattr(pm_transport.clob_http_helpers, "_http_client", old_client)
    monkeypatch.setattr(pm_transport, "_configured_proxy_url", None)

    class _HttpxClient:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def close(self):
            pass

    class _ClobClient:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(pm_transport.httpx, "Client", _HttpxClient)
    monkeypatch.setattr("nautilus_trader.adapters.polymarket.factories.ClobClient", _ClobClient)

    get_polymarket_http_client(
        api_key="K",
        api_secret="S",
        passphrase="P",
        private_key="0x" + "1" * 64,
        funder="0x" + "2" * 40,
        proxy_url="http://127.0.0.1:7890",
    )

    assert created == [{"http2": True, "proxy": "http://127.0.0.1:7890", "trust_env": False}]
    assert old_client.closed


def test_polymarket_geoblock_preflight_rejects_blocked_route(monkeypatch):
    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"blocked": True, "country": "AU", "region": "NSW"}

    class _HttpxClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            assert url == pm_transport.GEOBLOCK_URL
            return _Response()

    monkeypatch.setattr(pm_transport.httpx, "Client", _HttpxClient)

    try:
        pm_transport.check_polymarket_geoblock("http://127.0.0.1:7890")
    except RuntimeError as e:
        assert "geoblocked" in str(e)
        assert "country=AU" in str(e)
    else:
        raise AssertionError("blocked geoblock response should fail preflight")


def test_polymarket_geoblock_preflight_allows_frontend_only_restricted_country(monkeypatch):
    """官方文档列 JP 为 frontend-only restricted;API preflight 不应一刀切拦。"""
    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"blocked": True, "country": "JP", "region": "27"}

    class _HttpxClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return _Response()

    monkeypatch.setattr(pm_transport.httpx, "Client", _HttpxClient)

    assert pm_transport.check_polymarket_geoblock(None)["country"] == "JP"


def test_polymarket_geoblock_preflight_uses_configured_proxy(monkeypatch):
    created = []

    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"blocked": False, "country": "IE", "region": ""}

    class _HttpxClient:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return _Response()

    monkeypatch.setattr(pm_transport.httpx, "Client", _HttpxClient)

    assert pm_transport.check_polymarket_geoblock("http://127.0.0.1:7890")["blocked"] is False
    assert created == [{"proxy": "http://127.0.0.1:7890", "trust_env": False, "timeout": 10.0}]
