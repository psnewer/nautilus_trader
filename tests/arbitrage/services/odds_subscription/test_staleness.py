"""
测试赔率订阅的 staleness 检测和刷新逻辑

覆盖：
1. Service 层：Polymarket 和 OrbitExch 按 venue 独立检测超时
2. OrbitExch 客户端层：按页面独立检测超时
3. 心跳不应刷新数据时间戳
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.arbitrage.services.market_matching.service import MatchedPair
from src.arbitrage.services.odds_subscription.config import OddsSubscriptionConfig
from src.arbitrage.services.odds_subscription.service import OddsSubscriptionService
from src.arbitrage.services.odds_subscription.orbitexch_client import OrbitExchOddsClient


# =========================================================================
# Helpers
# =========================================================================

def _build_pair(pair_id: str, event_id: str) -> MatchedPair:
    return MatchedPair(
        pair_id=pair_id,
        sport="football",
        competition="Test League",
        polymarket_home_team="Team A",
        polymarket_away_team="Team B",
        polymarket_event_id=event_id,
        polymarket_data={"event_id": event_id},
        orbitexch_home_team="Team A",
        orbitexch_away_team="Team B",
        orbitexch_data={
            "market_id": "market-1",
            "sport_id": "1",
            "competition_id": "comp-1",
            "home_selection_id": "111",
            "away_selection_id": "222",
        },
        home_similarity=10,
        away_similarity=10,
        total_similarity=20,
        confidence=1.0,
    )


def _make_service(staleness_timeout: int = 10) -> OddsSubscriptionService:
    config = OddsSubscriptionConfig(staleness_timeout_sec=staleness_timeout)
    service = OddsSubscriptionService(config=config)
    service._polymarket_client = AsyncMock()
    service._orbitexch_client = AsyncMock()
    return service


def _subscribe_pair(service: OddsSubscriptionService, pair_id: str = "pair-1"):
    pair = _build_pair(pair_id, f"event-{pair_id}")
    service._subscribed_pairs[pair_id] = pair
    now = time.time()
    service._last_updates_pm[pair_id] = now
    service._last_updates_oe[pair_id] = now
    return pair


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# =========================================================================
# Service 层测试
# =========================================================================

class TestServiceStaleness:
    """Service 层按 venue 独立检测超时"""

    def test_no_staleness_when_both_fresh(self):
        """两个 venue 都新鲜时不触发刷新"""
        service = _make_service(staleness_timeout=10)
        _subscribe_pair(service)

        _run(service._check_staleness())

        service._polymarket_client.subscribe_event.assert_not_called()
        service._orbitexch_client.refresh_page.assert_not_called()

    def test_only_polymarket_stale(self):
        """只有 Polymarket 超时时，只刷新 Polymarket"""
        service = _make_service(staleness_timeout=10)
        pair = _subscribe_pair(service)

        service._last_updates_pm["pair-1"] = time.time() - 20
        service._last_updates_oe["pair-1"] = time.time()

        _run(service._check_staleness())

        service._polymarket_client.subscribe_event.assert_called_once_with(
            pair.polymarket_event_id
        )
        service._orbitexch_client.refresh_page.assert_not_called()

    def test_only_orbitexch_stale(self):
        """只有 OrbitExch 超时时，只刷新 OrbitExch"""
        service = _make_service(staleness_timeout=10)
        _subscribe_pair(service)

        service._last_updates_pm["pair-1"] = time.time()
        service._last_updates_oe["pair-1"] = time.time() - 20

        _run(service._check_staleness())

        service._polymarket_client.subscribe_event.assert_not_called()
        service._orbitexch_client.refresh_page.assert_called_once()

    def test_both_stale(self):
        """两个 venue 都超时时，两个都刷新"""
        service = _make_service(staleness_timeout=10)
        pair = _subscribe_pair(service)

        service._last_updates_pm["pair-1"] = time.time() - 20
        service._last_updates_oe["pair-1"] = time.time() - 20

        _run(service._check_staleness())

        service._polymarket_client.subscribe_event.assert_called_once()
        service._orbitexch_client.refresh_page.assert_called_once()

    def test_pm_update_does_not_reset_oe_timestamp(self):
        """Polymarket 更新不应重置 OrbitExch 的时间戳"""
        service = _make_service(staleness_timeout=10)
        pair = _subscribe_pair(service)

        old_time = time.time() - 20
        service._last_updates_pm["pair-1"] = old_time
        service._last_updates_oe["pair-1"] = old_time

        service._on_polymarket_update({
            "event_id": pair.polymarket_event_id,
            "market_type": "home",
            "bid": 0.5,
            "ask": 0.6,
        })

        assert service._last_updates_pm["pair-1"] > old_time
        assert service._last_updates_oe["pair-1"] == old_time

    def test_oe_update_does_not_reset_pm_timestamp(self):
        """OrbitExch 更新不应重置 Polymarket 的时间戳"""
        service = _make_service(staleness_timeout=10)
        pair = _subscribe_pair(service)

        old_time = time.time() - 20
        service._last_updates_pm["pair-1"] = old_time
        service._last_updates_oe["pair-1"] = old_time

        # OrbitExch 回调使用 pair_id + market_type
        service._on_orbitexch_update({
            "pair_id": "pair-1",
            "market_type": "home",
            "back": 2.0,
            "lay": 2.1,
        })

        assert service._last_updates_oe["pair-1"] > old_time
        assert service._last_updates_pm["pair-1"] == old_time

    def test_refresh_resets_only_target_venue(self):
        """_refresh_pair 只重置指定 venue 的时间戳"""
        service = _make_service(staleness_timeout=10)
        pair = _subscribe_pair(service)

        old_time = time.time() - 100
        service._last_updates_pm["pair-1"] = old_time
        service._last_updates_oe["pair-1"] = old_time

        _run(service._refresh_pair("pair-1", venue="polymarket"))

        assert service._last_updates_pm["pair-1"] > old_time
        assert service._last_updates_oe["pair-1"] == old_time

    def test_multi_pair_independent(self):
        """多个 pair 独立检测超时"""
        service = _make_service(staleness_timeout=10)
        pair1 = _subscribe_pair(service, "pair-1")
        pair2 = _subscribe_pair(service, "pair-2")

        service._last_updates_pm["pair-1"] = time.time() - 20
        service._last_updates_pm["pair-2"] = time.time()
        service._last_updates_oe["pair-1"] = time.time()
        service._last_updates_oe["pair-2"] = time.time()

        _run(service._check_staleness())

        service._polymarket_client.subscribe_event.assert_called_once_with(
            pair1.polymarket_event_id
        )
        service._orbitexch_client.refresh_page.assert_not_called()

    def test_get_subscriptions_per_venue(self):
        """get_subscriptions 返回按 venue 拆分的状态"""
        service = _make_service(staleness_timeout=10)
        _subscribe_pair(service)

        service._last_updates_pm["pair-1"] = time.time() - 5
        service._last_updates_oe["pair-1"] = time.time() - 15

        subs = service.get_subscriptions()
        assert len(subs) == 1
        sub = subs[0]

        assert "pm_last_update_sec_ago" in sub
        assert "oe_last_update_sec_ago" in sub
        assert "pm_stale" in sub
        assert "oe_stale" in sub

        assert sub["pm_stale"] is False
        assert sub["oe_stale"] is True


# =========================================================================
# OrbitExch 客户端层测试
# =========================================================================

class TestOrbitExchStaleness:
    """OrbitExch 客户端按页面独立检测超时"""

    def _make_client(self, staleness_timeout: int = 5) -> OrbitExchOddsClient:
        config = OddsSubscriptionConfig(
            orbitexch_staleness_timeout_sec=staleness_timeout,
        )
        client = OrbitExchOddsClient(config=config)
        return client

    def test_heartbeat_does_not_update_timestamp(self):
        """心跳消息 'h' 不应更新数据时间戳"""
        client = self._make_client()
        client._last_data_updates["page-1"] = 1000.0

        client._on_websocket_frame(
            {"response": {"payloadData": "h"}},
            page_key="page-1",
        )

        assert client._last_data_updates["page-1"] == 1000.0

    def test_open_message_does_not_update_timestamp(self):
        """SockJS open 消息 'o' 不应更新数据时间戳"""
        client = self._make_client()
        client._last_data_updates["page-1"] = 1000.0

        client._on_websocket_frame(
            {"response": {"payloadData": "o"}},
            page_key="page-1",
        )

        assert client._last_data_updates["page-1"] == 1000.0

    def test_data_message_updates_only_its_page(self):
        """实际数据消息 a[...] 只更新对应页面的时间戳"""
        client = self._make_client()
        client._last_data_updates["page-1"] = 1000.0
        client._last_data_updates["page-2"] = 1000.0

        client._on_websocket_frame(
            {"response": {"payloadData": 'a["{}"]'}},
            page_key="page-1",
        )

        assert client._last_data_updates["page-1"] > 1000.0
        assert client._last_data_updates["page-2"] == 1000.0

    def test_only_stale_page_refreshed(self):
        """只刷新超时的页面，不影响正常的页面"""
        client = self._make_client(staleness_timeout=5)

        mock_page_1 = AsyncMock()
        mock_page_2 = AsyncMock()
        client._pages = {"page-1": mock_page_1, "page-2": mock_page_2}
        client._running = True

        now_ms = time.time() * 1000
        client._last_data_updates["page-1"] = now_ms - 10_000  # 10 秒前
        client._last_data_updates["page-2"] = now_ms  # 刚更新

        client._refresh_single_page = AsyncMock()

        _run(client._check_and_refresh_if_stale())

        client._refresh_single_page.assert_called_once_with("page-1", mock_page_1)

    def test_no_refresh_when_all_fresh(self):
        """所有页面都新鲜时不刷新"""
        client = self._make_client(staleness_timeout=5)

        mock_page = AsyncMock()
        client._pages = {"page-1": mock_page}
        client._running = True
        client._last_data_updates["page-1"] = time.time() * 1000

        client._refresh_single_page = AsyncMock()

        _run(client._check_and_refresh_if_stale())

        client._refresh_single_page.assert_not_called()

    def test_skip_pages_with_no_data_yet(self):
        """从未收到数据的页面不触发刷新（初始化阶段）"""
        client = self._make_client(staleness_timeout=5)

        mock_page = AsyncMock()
        client._pages = {"page-1": mock_page}
        client._running = True

        client._refresh_single_page = AsyncMock()

        _run(client._check_and_refresh_if_stale())

        client._refresh_single_page.assert_not_called()

    def test_main_page_skipped(self):
        """main 页面始终跳过"""
        client = self._make_client(staleness_timeout=5)

        mock_main = AsyncMock()
        client._pages = {"main": mock_main}
        client._running = True
        client._last_data_updates["main"] = 1000.0

        client._refresh_single_page = AsyncMock()

        _run(client._check_and_refresh_if_stale())

        client._refresh_single_page.assert_not_called()

    def test_refresh_page_specific(self):
        """refresh_page 可以只刷新指定页面"""
        client = self._make_client()

        mock_page_1 = AsyncMock()
        mock_page_2 = AsyncMock()
        client._pages = {"page-1": mock_page_1, "page-2": mock_page_2}
        client._refresh_single_page = AsyncMock()

        _run(client.refresh_page(target_page_key="page-1"))

        client._refresh_single_page.assert_called_once_with("page-1", mock_page_1)

    def test_refresh_page_all(self):
        """refresh_page 不指定 page_key 时刷新所有非 main 页面"""
        client = self._make_client()

        mock_main = AsyncMock()
        mock_page_1 = AsyncMock()
        mock_page_2 = AsyncMock()
        client._pages = {"main": mock_main, "page-1": mock_page_1, "page-2": mock_page_2}
        client._refresh_single_page = AsyncMock()

        _run(client.refresh_page())

        assert client._refresh_single_page.call_count == 2
        call_keys = [call.args[0] for call in client._refresh_single_page.call_args_list]
        assert "page-1" in call_keys
        assert "page-2" in call_keys
        assert "main" not in call_keys

    def test_get_cdp_status_per_page(self):
        """get_cdp_status 返回按页面拆分的状态"""
        client = self._make_client()
        now_ms = time.time() * 1000
        client._last_data_updates["page-1"] = now_ms - 5000
        client._last_data_updates["page-2"] = now_ms - 30000

        status = client.get_cdp_status()

        assert "last_data_updates" in status
        assert "page-1" in status["last_data_updates"]
        assert "page-2" in status["last_data_updates"]

        assert 3 < status["last_data_updates"]["page-1"]["age_sec"] < 8
        assert 28 < status["last_data_updates"]["page-2"]["age_sec"] < 35
