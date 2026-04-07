"""
赔率订阅服务

协调 Polymarket 和 OrbitExch 客户端，管理赔率订阅生命周期。
"""

import asyncio
import logging
import time
from typing import Any, Callable

from src.arbitrage.services.market_matching.service import MatchedPair

from .config import OddsSubscriptionConfig
from .messages import OddsUpdateMessage, MatchStatusMessage, PairActivityMessage
from .orbitexch_client import OrbitExchOddsClient
from .polymarket_client import PolymarketOddsClient
from .topics import (
    odds_topic,
    match_status_topic,
    PAIR_ACTIVITY_TOPIC_PATTERN,
)


class OddsSubscriptionService:
    """
    赔率订阅服务

    功能：
    1. 基于 matched_pairs 批量订阅赔率
    2. 管理 Polymarket 和 OrbitExch 客户端
    3. 超时监控和自动刷新
    4. 提供最新赔率数据
    """

    def __init__(
        self,
        config: OddsSubscriptionConfig | None = None,
        logger: logging.Logger | None = None,
        msgbus=None,
    ):
        self.config = config or OddsSubscriptionConfig()
        self._log = logger or logging.getLogger(self.__class__.__name__)
        self._msgbus = None

        # 客户端
        self._polymarket_client: PolymarketOddsClient | None = None
        self._orbitexch_client: OrbitExchOddsClient | None = None

        # RiskService 引用（用于余额回调）
        self._risk_service = None

        # 订阅状态
        self._subscribed_pairs: dict[str, MatchedPair] = {}  # pair_id -> MatchedPair
        self._latest_odds: dict[str, dict] = {}  # pair_id -> {polymarket: {...}, orbitexch: {...}}
        self._pair_activity: dict[str, float] = {}  # pair_id -> last_active_ts

        # 超时监控（按 venue 独立追踪）
        self._last_updates_pm: dict[str, float] = {}  # pair_id -> timestamp (Polymarket)
        self._last_updates_oe: dict[str, float] = {}  # pair_id -> timestamp (OrbitExch)
        self._heartbeat_task: asyncio.Task | None = None

        # 运行状态
        self._running = False

        # 仓位变化回调
        self._position_callbacks: list[Callable[[str], None]] = []
        self._polymarket_order_callbacks: list[Callable[[str, dict], None]] = []
        self._orbitexch_bets_callbacks: list[Callable[[str, dict], None]] = []

        if msgbus is not None:
            self.set_msgbus(msgbus)

    # =========================================================================
    # 生命周期
    # =========================================================================

    async def start(self) -> None:
        """启动服务"""
        if self._running:
            self._log.warning("Service already running")
            return

        self._log.info("Starting odds subscription service...")

        self._ensure_clients_created()
        await self.ensure_polymarket_client_ready()

        # 启动客户端
        await self._polymarket_client.start()
        await self._orbitexch_client.start()

        # 启动超时监控
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        self._running = True
        self._log.info("Odds subscription service started")

    async def stop(self) -> None:
        """停止服务"""
        if not self._running:
            return

        self._log.info("Stopping odds subscription service...")

        # 停止超时监控
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # 停止客户端
        if self._polymarket_client:
            await self._polymarket_client.stop()

        if self._orbitexch_client:
            await self._orbitexch_client.stop()

        self._running = False
        self._log.info("Odds subscription service stopped")

    def update_config(self, config: OddsSubscriptionConfig) -> None:
        """
        更新服务配置

        注意：部分配置（如凭据）可能需要重启服务才能生效。
        超时相关配置会立即生效。

        Args:
            config: 新的配置
        """
        self.config = config
        if self._polymarket_client:
            self._polymarket_client.config = config
        if self._orbitexch_client:
            self._orbitexch_client.config = config
        self._log.info(
            f"Odds config updated: staleness_timeout={config.staleness_timeout_sec}s, "
            f"orbitexch_staleness_timeout={config.orbitexch_staleness_timeout_sec}s"
        )

    def _ensure_clients_created(self) -> None:
        """确保共享客户端实例已创建并绑定回调。"""
        if self._polymarket_client is None:
            self._polymarket_client = PolymarketOddsClient(
                config=self.config,
                logger=logging.getLogger("PolymarketOdds"),
            )
            self._polymarket_client.on_price_update(self._on_polymarket_update)
            self._polymarket_client.register_positions_callback(self._on_polymarket_position_update)
            self._polymarket_client.register_orders_callback(self._on_polymarket_orders_update)

        if self._orbitexch_client is None:
            self._orbitexch_client = OrbitExchOddsClient(
                config=self.config,
                logger=logging.getLogger("OrbitExchOdds"),
            )
            self._orbitexch_client.on_price_update(self._on_orbitexch_update)
            self._orbitexch_client.on_page_refresh(self._on_orbitexch_page_refresh)
            self._orbitexch_client.register_status_callback(self._on_match_status_update)
            self._orbitexch_client.register_bets_callback(self._on_orbitexch_bets_update)
            self._orbitexch_client.register_balance_callback(self._on_orbitexch_balance_update)

    async def ensure_polymarket_client_ready(self) -> bool:
        """确保 Polymarket 共享客户端已创建且具备下单能力。"""
        self._ensure_clients_created()
        if not self._polymarket_client:
            return False
        if self._polymarket_client._clob_client:
            return True

        ok = self._polymarket_client.initialize_clob_client()
        if ok:
            self._log.info("Polymarket client ready")
        else:
            self._log.warning("Polymarket client initialization failed")
        return ok

    # =========================================================================
    # 订阅管理
    # =========================================================================

    async def subscribe_matched_pairs(self, matched_pairs: list[MatchedPair]) -> None:
        """
        订阅 matched pairs 的赔率（并行处理）

        使用稳定的 pair_id（基于 polymarket_event_id），重新订阅不会导致 ID 不匹配。

        Args:
            matched_pairs: 匹配的市场对列表
        """
        if not self._running:
            await self.start()

        self._log.info(f"Subscribing to {len(matched_pairs)} matched pairs in parallel")

        # 清理不在新列表中的旧订阅
        new_pair_ids = {p.pair_id for p in matched_pairs}
        stale_ids = [pid for pid in self._subscribed_pairs if pid not in new_pair_ids]
        for pid in stale_ids:
            self._subscribed_pairs.pop(pid, None)
            self._latest_odds.pop(pid, None)
            self._last_updates_pm.pop(pid, None)
            self._last_updates_oe.pop(pid, None)
            self._pair_activity.pop(pid, None)
            self._log.info(f"Removed stale subscription: {pid}")

        # 并行订阅所有 pairs
        tasks = [
            self._subscribe_single_pair_safe(pair)
            for pair in matched_pairs
        ]
        await asyncio.gather(*tasks)

        self._log.info(f"Subscription complete: {len(self._subscribed_pairs)} pairs")

        # 关闭 OrbitExch 主页面（释放资源）
        if self._orbitexch_client:
            await self._orbitexch_client.close_main_page()

    async def _subscribe_single_pair_safe(self, pair: MatchedPair) -> None:
        """安全地订阅单个 pair（捕获异常）"""
        try:
            await self._subscribe_single_pair(pair)
        except Exception as e:
            self._log.error(f"Failed to subscribe pair {pair.pair_id}: {e}")

    async def _subscribe_single_pair(self, pair: MatchedPair) -> None:
        """
        订阅单个 matched pair

        Args:
            pair: MatchedPair 实例
        """
        pair_id = pair.pair_id

        # 记录订阅
        self._subscribed_pairs[pair_id] = pair
        self._last_updates_pm[pair_id] = time.time()
        self._last_updates_oe[pair_id] = time.time()

        # 订阅 Polymarket
        polymarket_event_id = pair.polymarket_event_id
        if polymarket_event_id and self._polymarket_client:
            await self._polymarket_client.subscribe_event(polymarket_event_id)

        # 订阅 OrbitExch
        # 从 orbitexch_data 提取 sport_id 和 competition_id
        orbitexch_data = pair.orbitexch_data
        sport_id = orbitexch_data.get("sport_id", "")
        competition_id = orbitexch_data.get("competition_id", "")

        self._log.info(
            f"OrbitExch data for pair {pair_id}: "
            f"sport_id='{sport_id}', competition_id='{competition_id}', "
            f"full_data={orbitexch_data}"
        )

        if sport_id and competition_id and self._orbitexch_client:
            # 首先注册 pair 信息（队名），用于在订阅时通过队名重新匹配
            self._orbitexch_client.register_pair_info(
                pair_id=pair_id,
                home_team=pair.orbitexch_home_team,
                away_team=pair.orbitexch_away_team,
            )

            # 注册 (market_id, selection_id) -> (pair_id, market_type) 映射
            # 注意：这些 market_id 可能在订阅时被更新（因为页面内容可能变化）
            market_id = orbitexch_data.get("market_id", "")
            home_sel_id = orbitexch_data.get("home_selection_id", "")
            draw_sel_id = orbitexch_data.get("draw_selection_id", "")
            away_sel_id = orbitexch_data.get("away_selection_id", "")

            if not market_id:
                self._log.warning(
                    f"Missing market_id for pair {pair_id}, will try to match by team names during subscription"
                )

            if market_id and home_sel_id:
                self._orbitexch_client.register_selection(market_id, home_sel_id, pair_id, "home")
            if market_id and draw_sel_id:
                self._orbitexch_client.register_selection(market_id, draw_sel_id, pair_id, "draw")
            if market_id and away_sel_id:
                self._orbitexch_client.register_selection(market_id, away_sel_id, pair_id, "away")

            self._log.info(
                f"Registered OrbitExch selections for {pair_id}: "
                f"market_id={market_id}, home={home_sel_id}, draw={draw_sel_id}, away={away_sel_id}"
            )

            # 为这个 pair 订阅 OrbitExch
            await self._orbitexch_client.subscribe_competition(
                sport_id=sport_id,
                competition_id=competition_id,
                event_ids=[pair_id],
            )
        else:
            self._log.warning(
                f"Cannot subscribe OrbitExch for pair {pair_id}: "
                f"missing sport_id='{sport_id}' or competition_id='{competition_id}'"
            )

        self._log.info(f"Subscribed pair {pair_id}: {pair.sport} - {pair.competition}")

    # =========================================================================
    # 数据回调
    # =========================================================================

    def _on_polymarket_update(self, odds_data: dict) -> None:
        """
        Polymarket 价格更新回调

        Args:
            odds_data: 赔率数据
        """
        event_id = odds_data.get("event_id")
        market_type = odds_data.get("market_type")

        # 查找对应的 pair_id
        for pair_id, pair in self._subscribed_pairs.items():
            if pair.polymarket_event_id == event_id:
                # 更新缓存
                self._latest_odds.setdefault(pair_id, {"polymarket": {}, "orbitexch": {}})
                self._latest_odds[pair_id].setdefault("polymarket", {})

                # price_change 消息不含 size，保留缓存的值
                if odds_data.get("source") == "price_change":
                    cached = self._latest_odds[pair_id]["polymarket"].get(market_type, {})
                    if "bid_size" not in odds_data and "bid_size" in cached:
                        odds_data["bid_size"] = cached["bid_size"]
                    if "ask_size" not in odds_data and "ask_size" in cached:
                        odds_data["ask_size"] = cached["ask_size"]
                self._latest_odds[pair_id]["polymarket"][market_type] = odds_data

                # 更新时间戳
                self._last_updates_pm[pair_id] = time.time()

                self._log.debug(
                    f"Polymarket: {pair_id} {market_type} "
                    f"bid={odds_data.get('bid')} ask={odds_data.get('ask')}"
                )

                # 发布赔率更新
                self._publish_odds_update(pair_id, "polymarket", odds_data)
                break

    def _on_orbitexch_page_refresh(self) -> None:
        """OrbitExch 页面刷新回调：清除所有 OrbitExch 赔率缓存"""
        for pair_id in list(self._latest_odds.keys()):
            if "orbitexch" in self._latest_odds[pair_id]:
                self._latest_odds[pair_id]["orbitexch"] = {}
        self._log.info("Cleared OrbitExch odds cache due to page refresh")

    def _on_orbitexch_update(self, odds_data: dict) -> None:
        """
        OrbitExch 价格更新回调

        新数据格式（使用 selection_id 映射）:
        {
            "pair_id": "pair-xxx",
            "selection_id": "39674645",
            "market_type": "home",  # home/draw/away
            "back": 2.06,
            "lay": 2.10,
            "timestamp": 1234567890
        }
        """
        pair_id = odds_data.get("pair_id")
        market_type = odds_data.get("market_type", "")
        back = odds_data.get("back", 0)
        lay = odds_data.get("lay", 0)

        if pair_id and pair_id in self._subscribed_pairs:
            # 更新缓存
            self._latest_odds.setdefault(pair_id, {"polymarket": {}, "orbitexch": {}})
            self._latest_odds[pair_id].setdefault("orbitexch", {})

            self._latest_odds[pair_id]["orbitexch"][market_type] = {
                "market_type": market_type,
                "back": back,
                "lay": lay,
                "back_size": odds_data.get("back_size", 0),
                "lay_size": odds_data.get("lay_size", 0),
                "timestamp": odds_data.get("timestamp"),
            }

            # 更新时间戳
            self._last_updates_oe[pair_id] = time.time()

            self._log.debug(
                f"OrbitExch: {pair_id} {market_type} back={back} lay={lay}"
            )

            # 发布赔率更新
            self._publish_odds_update(
                pair_id,
                "orbitexch",
                self._latest_odds[pair_id]["orbitexch"][market_type],
            )

    # =========================================================================
    # 消息总线
    # =========================================================================

    def set_msgbus(self, msgbus) -> None:
        """设置消息总线"""
        self._msgbus = msgbus
        if self._msgbus:
            self._msgbus.subscribe(PAIR_ACTIVITY_TOPIC_PATTERN, self._on_pair_activity_message)

    def set_risk_service(self, risk_service) -> None:
        """设置 RiskService 引用（用于余额回调）"""
        self._risk_service = risk_service

    def _publish_odds_update(self, pair_id: str, venue: str, odds_data: dict) -> None:
        """发布赔率更新"""
        if not self._msgbus:
            return
        if self._is_pair_active(pair_id):
            return
        odds_data = self._apply_mock_odds(pair_id, venue, odds_data)
        market_type = odds_data.get("market_type", "")
        if not market_type:
            return
        msg = OddsUpdateMessage(
            pair_id=pair_id,
            venue=venue,
            market_type=market_type,
            odds_data=odds_data,
        )
        self._msgbus.publish(odds_topic(venue, pair_id, market_type), msg, external_pub=False)

    def _apply_mock_odds(self, pair_id: str, venue: str, odds_data: dict) -> dict:
        """应用 debug mock 赔率覆盖"""
        try:
            from src.arbitrage.services.debug import debug_manager, MockCategory
        except ImportError:
            return odds_data
        if not debug_manager.enabled:
            return odds_data
        market_type = odds_data.get("market_type", "")
        context = {"pair_id": pair_id, "venue": venue, "market_type": market_type}
        mocked = debug_manager.get_mock_data(MockCategory.ODDS, context)
        if isinstance(mocked, dict) and mocked:
            return mocked
        return odds_data

    def _on_match_status_update(self, pair_id: str, is_live: bool) -> None:
        """发布比赛状态更新"""
        if not self._msgbus:
            return
        msg = MatchStatusMessage(pair_id=pair_id, is_live=is_live, source="orbitexch")
        self._msgbus.publish(match_status_topic(pair_id), msg, external_pub=False)

    def _on_pair_activity_message(self, msg: Any) -> None:
        """处理 pair 活跃状态消息"""
        if isinstance(msg, PairActivityMessage):
            pair_id = msg.pair_id
            is_active = msg.is_active
        elif isinstance(msg, dict):
            pair_id = msg.get("pair_id", "")
            is_active = msg.get("is_active", False)
        else:
            return

        if not pair_id:
            return

        if is_active:
            self._pair_activity[pair_id] = time.time()
        else:
            self._pair_activity.pop(pair_id, None)

    def _is_pair_active(self, pair_id: str) -> bool:
        """检查 pair 是否处于活跃互斥状态"""
        last_active = self._pair_activity.get(pair_id)
        if last_active is None:
            return False

        timeout_sec = self.config.pair_activity_timeout_sec
        if timeout_sec > 0 and time.time() - last_active > timeout_sec:
            self._pair_activity.pop(pair_id, None)
            return False

        return True

    # =========================================================================
    # 仓位变化回调
    # =========================================================================

    def register_position_callback(
        self,
        callback: Callable[[str], None],
    ) -> None:
        """
        注册仓位变化回调

        当 Polymarket 或 OrbitExch 仓位变化时触发。

        Args:
            callback: 回调函数，签名为 callback(pair_id)
        """
        self._position_callbacks.append(callback)
        self._log.info(f"Position callback registered (total: {len(self._position_callbacks)})")

    def _notify_position_callbacks(self, pair_id: str) -> None:
        """
        通知所有仓位变化回调

        Args:
            pair_id: 比赛 ID
        """
        for callback in self._position_callbacks:
            try:
                callback(pair_id)
            except Exception as e:
                self._log.error(f"Position callback error: {e}")

    def register_polymarket_order_callback(
        self,
        callback: Callable[[str, dict], None],
    ) -> None:
        """注册 Polymarket 订单更新回调"""
        if callback in self._polymarket_order_callbacks:
            return
        self._polymarket_order_callbacks.append(callback)

    def register_orbitexch_bets_callback(
        self,
        callback: Callable[[str, dict], None],
    ) -> None:
        """注册 OrbitExch 订单更新回调"""
        if callback in self._orbitexch_bets_callbacks:
            return
        self._orbitexch_bets_callbacks.append(callback)

    def _notify_polymarket_order_callbacks(self, event_type: str, data: dict) -> None:
        for callback in self._polymarket_order_callbacks:
            try:
                callback(event_type, data)
            except Exception as e:
                self._log.error(f"Polymarket order callback error: {e}")

    def _notify_orbitexch_bets_callbacks(self, event_type: str, data: dict) -> None:
        for callback in self._orbitexch_bets_callbacks:
            try:
                callback(event_type, data)
            except Exception as e:
                self._log.error(f"OrbitExch bets callback error: {e}")

    def _on_orbitexch_bets_update(self, bets_data: dict) -> None:
        """
        OrbitExch 订单更新回调

        Args:
            bets_data: {"market_id": str, "bets": list}
        """
        market_id = bets_data.get("market_id", "")

        # 查找对应的 pair_id
        if market_id:
            for pair_id, pair in self._subscribed_pairs.items():
                orbitexch_data = pair.orbitexch_data
                if orbitexch_data.get("market_id") == market_id:
                    self._log.debug(f"OrbitExch bets update for {pair_id}")
                    self._notify_position_callbacks(pair_id)
                    break

        for bet in bets_data.get("bets", []):
            self._notify_orbitexch_bets_callbacks(
                "UPDATE",
                {
                    "offerId": bet.get("offerId"),
                    "marketId": bet.get("marketId"),
                    "selectionId": bet.get("selectionId"),
                    "sizeMatched": bet.get("sizeMatched", 0),
                    "sizeRemaining": bet.get("sizeRemaining", 0),
                },
            )

    def _on_polymarket_orders_update(self, data: dict) -> None:
        """
        Polymarket 订单更新回调

        处理两种消息类型：
        - type == "order": 订单事件（PLACEMENT / UPDATE / CANCELLATION）
        - type == "trade": 成交事件（含 CONFIRMED 终态信号）

        Args:
            data: 订单/成交更新数据
        """
        msg_type = data.get("type")

        if msg_type == "order":
            order = data.get("order")
            if not order:
                return

            # 使用 order_type 字段（由 polymarket_client 从 WS 的 type 映射而来）
            order_type = data.get("order_type", "")
            if order_type == "CANCELLATION":
                event_type = "CANCELLATION"
            elif order_type == "UPDATE":
                event_type = "UPDATE"
            else:
                event_type = "PLACEMENT"

            self._notify_polymarket_order_callbacks(
                event_type,
                {
                    "id": getattr(order, "order_id", ""),
                    "order_id": getattr(order, "order_id", ""),
                    "size_matched": getattr(order, "size_matched", 0),
                    "original_size": getattr(order, "original_size", 0),
                },
            )

        elif msg_type == "trade":
            trade = data.get("trade")
            if not trade:
                return

            status = trade.get("status", "")
            if status == "CONFIRMED":
                # CONFIRMED 是完全成交的终态信号
                maker_orders = trade.get("maker_orders", [])
                self._notify_polymarket_order_callbacks(
                    "TRADE_CONFIRMED",
                    {
                        "maker_orders": maker_orders,
                        "taker_order_id": trade.get("taker_order_id", ""),
                        "size": trade.get("size", "0"),
                    },
                )
            elif status == "FAILED":
                self._log.warning(
                    f"Trade FAILED: id={trade.get('id', '')}"
                )

    def _on_polymarket_position_update(self, event_id: str) -> None:
        """
        Polymarket 仓位更新回调

        Args:
            event_id: Polymarket event ID
        """
        # 查找对应的 pair_id
        for pair_id, pair in self._subscribed_pairs.items():
            if pair.polymarket_event_id == event_id:
                self._log.debug(f"Polymarket position update for {pair_id}")
                self._notify_position_callbacks(pair_id)
                break

    def _on_orbitexch_balance_update(self, balance: float) -> None:
        """
        OrbitExch 余额更新回调

        Args:
            balance: OrbitExch 余额
        """
        if self._risk_service:
            try:
                self._risk_service.update_orbitexch_balance(balance)
            except Exception as e:
                self._log.error(f"Failed to update OrbitExch balance in RiskService: {e}")

    # =========================================================================
    # 超时监控
    # =========================================================================

    async def _heartbeat_loop(self) -> None:
        """
        超时监控循环

        定期检查数据更新时间，超时则刷新
        """
        self._log.info("Starting heartbeat monitor")

        while True:
            try:
                await self._check_staleness()
                await asyncio.sleep(30)  # 每30秒检查一次

            except asyncio.CancelledError:
                self._log.info("Heartbeat monitor cancelled")
                break
            except Exception as e:
                self._log.error(f"Error in heartbeat monitor: {e}")
                await asyncio.sleep(30)

    async def _check_staleness(self) -> None:
        """按 venue 独立检查数据新鲜度"""
        now = time.time()
        timeout_sec = self.config.staleness_timeout_sec

        for pair_id in list(self._subscribed_pairs.keys()):
            # 检查 Polymarket
            pm_last = self._last_updates_pm.get(pair_id, 0)
            if pm_last > 0 and now - pm_last > timeout_sec:
                self._log.warning(
                    f"Polymarket stale for pair {pair_id}: "
                    f"{int(now - pm_last)}s since last update"
                )
                await self._refresh_pair(pair_id, venue="polymarket")

            # 检查 OrbitExch
            oe_last = self._last_updates_oe.get(pair_id, 0)
            if oe_last > 0 and now - oe_last > timeout_sec:
                self._log.warning(
                    f"OrbitExch stale for pair {pair_id}: "
                    f"{int(now - oe_last)}s since last update"
                )
                await self._refresh_pair(pair_id, venue="orbitexch")

    async def _refresh_pair(self, pair_id: str, venue: str | None = None) -> None:
        """
        刷新 pair 的订阅

        Args:
            pair_id: pair ID
            venue: 指定刷新的平台（polymarket/orbitexch），None 则两者都刷新
        """
        pair = self._subscribed_pairs.get(pair_id)
        if not pair:
            return

        self._log.info(f"Refreshing pair {pair_id} venue={venue or 'all'}")

        # 清空对应 venue 的赔率缓存，避免刷新期间使用过时数据
        if pair_id in self._latest_odds:
            if venue in (None, "polymarket"):
                self._latest_odds[pair_id].pop("polymarket", None)
            if venue in (None, "orbitexch"):
                self._latest_odds[pair_id].pop("orbitexch", None)

        # Polymarket: 重新订阅
        if venue in (None, "polymarket"):
            if pair.polymarket_event_id and self._polymarket_client:
                await self._polymarket_client.subscribe_event(pair.polymarket_event_id)
            self._last_updates_pm[pair_id] = time.time()

        # OrbitExch: 刷新页面（不关闭浏览器）
        if venue in (None, "orbitexch"):
            if self._orbitexch_client:
                await self._orbitexch_client.refresh_page()
            self._last_updates_oe[pair_id] = time.time()

    # =========================================================================
    # 数据访问
    # =========================================================================

    def get_latest_odds(self, pair_id: str | None = None) -> dict[str, Any]:
        """
        获取最新赔率数据

        Args:
            pair_id: pair ID，None 表示返回所有

        Returns:
            赔率数据字典
        """
        if pair_id:
            return self._latest_odds.get(pair_id, {})
        else:
            return self._latest_odds

    def get_pair_odds(self, pair_id: str) -> dict[str, Any]:
        """获取指定 pair 的最新赔率"""
        return self._latest_odds.get(pair_id, {})

    def get_subscriptions(self) -> list[dict[str, Any]]:
        """
        获取订阅状态

        Returns:
            订阅列表
        """
        subscriptions = []

        for pair_id, pair in self._subscribed_pairs.items():
            now = time.time()
            pm_last = self._last_updates_pm.get(pair_id, 0)
            oe_last = self._last_updates_oe.get(pair_id, 0)
            pm_age = int(now - pm_last) if pm_last > 0 else None
            oe_age = int(now - oe_last) if oe_last > 0 else None
            timeout = self.config.staleness_timeout_sec

            subscriptions.append({
                "pair_id": pair_id,
                "sport": pair.sport,
                "competition": pair.competition,
                "polymarket_event_id": pair.polymarket_event_id,
                "pm_last_update_sec_ago": pm_age,
                "oe_last_update_sec_ago": oe_age,
                "pm_stale": pm_age > timeout if pm_age else False,
                "oe_stale": oe_age > timeout if oe_age else False,
            })

        return subscriptions

    def get_match_status(self, pair_id: str) -> bool | None:
        """
        获取比赛状态（是否为赛中盘）

        通过 OrbitExch 页面的 data-time-section 属性判断:
        - inPlay: 赛中盘 (is_live=True)
        - comingUp: 赛前盘 (is_live=False)

        Args:
            pair_id: 比赛 ID

        Returns:
            True=赛中盘, False=赛前盘, None=未知
        """
        if self._orbitexch_client:
            return self._orbitexch_client.get_match_status(pair_id)
        return None

    def get_all_match_statuses(self) -> dict[str, bool]:
        """
        获取所有比赛的状态

        Returns:
            {pair_id: is_live}
        """
        if self._orbitexch_client:
            return self._orbitexch_client.get_all_match_statuses()
        return {}

    def get_pairs_info(self) -> dict[str, dict]:
        """
        获取所有订阅的 pairs 信息（包括队名等）

        Returns:
            {pair_id: {
                "polymarket_home": str,
                "polymarket_away": str,
                "orbitexch_home": str,
                "orbitexch_away": str,
                "competition": str,
                "is_live": bool | None,
            }}
        """
        result = {}
        for pair_id, pair in self._subscribed_pairs.items():
            is_live = self.get_match_status(pair_id)
            result[pair_id] = {
                "polymarket_home": pair.polymarket_home_team,
                "polymarket_away": pair.polymarket_away_team,
                "orbitexch_home": pair.orbitexch_home_team,
                "orbitexch_away": pair.orbitexch_away_team,
                "competition": pair.competition,
                "is_live": is_live,
            }
        return result

    # =========================================================================
    # 持仓查询
    # =========================================================================

    def get_polymarket_positions(self, pair_id: str | None = None) -> list:
        """
        获取 Polymarket 持仓

        Args:
            pair_id: 比赛 ID，None 表示获取所有

        Returns:
            PolymarketPosition 列表
        """
        if self._use_mock_exchange():
            from src.arbitrage.services.debug import debug_manager, MockCategory
            context = {"venue": "polymarket"}
            if pair_id:
                context["pair_id"] = pair_id
            return debug_manager.get_mock_data(MockCategory.POSITIONS, context) or []

        if not self._polymarket_client:
            return []
        return self._polymarket_client.get_positions_by_pair(pair_id) if pair_id else self._polymarket_client.get_positions()

    def get_polymarket_open_orders(self) -> list:
        """
        获取 Polymarket 当前未完成订单

        Returns:
            PolymarketOrder 列表
        """
        if self._use_mock_exchange():
            from src.arbitrage.services.debug import debug_manager, MockCategory
            context = {"venue": "polymarket"}
            return debug_manager.get_mock_data(MockCategory.ORDERS, context) or []

        if not self._polymarket_client:
            return []
        return self._polymarket_client.get_current_orders()

    async def wait_for_polymarket_positions(self, timeout: float) -> bool:
        """等待 Polymarket 首次 positions 到达"""
        if not self._polymarket_client:
            return False
        return await self._polymarket_client.wait_for_positions(timeout)

    def get_orbitexch_bets(self, pair_id: str | None = None) -> list:
        """
        获取 OrbitExch 当前订单

        Args:
            pair_id: 比赛 ID，None 表示获取所有

        Returns:
            OrbitExch bet 数据列表
        """
        if self._use_mock_exchange():
            from src.arbitrage.services.debug import debug_manager, MockCategory
            context = {"venue": "orbitexch"}
            if pair_id:
                context["pair_id"] = pair_id
            return debug_manager.get_mock_data(MockCategory.ORDERS, context) or []

        if not self._orbitexch_client:
            return []
        if pair_id:
            return self._orbitexch_client.get_bets_by_pair(pair_id)
        return self._orbitexch_client.get_current_bets()

    def get_orbitexch_open_orders(self) -> list[dict]:
        """
        获取 OrbitExch 当前活跃订单（sizeRemaining > 0）

        Returns:
            活跃订单列表
        """
        if self._use_mock_exchange():
            from src.arbitrage.services.debug import debug_manager, MockCategory
            context = {"venue": "orbitexch"}
            return debug_manager.get_mock_data(MockCategory.ORDERS, context) or []

        if not self._orbitexch_client:
            return []
        return self._orbitexch_client.get_active_orders()

    async def wait_for_orbitexch_current_bets(self, timeout: float) -> bool:
        """等待 OrbitExch 首次 CURRENT_BETS 到达"""
        if not self._orbitexch_client:
            return False
        return await self._orbitexch_client.wait_for_current_bets(timeout)

    @staticmethod
    def _use_mock_exchange() -> bool:
        """判断是否启用模拟交易所"""
        try:
            from src.arbitrage.services.debug import debug_manager
            return debug_manager.enabled and debug_manager.is_override_active("use_mock_exchange")
        except ImportError:
            return False

    def get_selection_mapping(self, pair_id: str) -> dict[int, str]:
        """
        获取 OrbitExch selection_id -> outcome 映射

        Args:
            pair_id: 比赛 ID

        Returns:
            {selection_id: outcome} 例: {123: "home", 456: "away"}
        """
        if not self._orbitexch_client:
            return {}
        return self._orbitexch_client.get_selection_mapping(pair_id)

    def get_polymarket_client(self):
        """获取 Polymarket 客户端（用于执行追踪）"""
        self._ensure_clients_created()
        return self._polymarket_client

    def get_orbitexch_client(self):
        """获取 OrbitExch 客户端（用于执行追踪）"""
        self._ensure_clients_created()
        return self._orbitexch_client

    def get_position_mappings(self) -> dict:
        """
        获取持仓数据加载所需的映射

        用于 RiskService 加载历史持仓。

        Returns:
            {
                "polymarket_pair_mapping": {event_id: pair_id},
                "orbitexch_pair_mapping": {market_id: pair_id},
                "selection_mappings": {pair_id: {selection_id: market_type}},
            }
        """
        polymarket_pair_mapping = {}  # event_id -> pair_id
        orbitexch_pair_mapping = {}   # market_id -> pair_id
        selection_mappings = {}       # pair_id -> {selection_id: market_type}

        for pair_id, pair in self._subscribed_pairs.items():
            # Polymarket: event_id -> pair_id
            if pair.polymarket_event_id:
                polymarket_pair_mapping[pair.polymarket_event_id] = pair_id

            # OrbitExch: market_id -> pair_id
            orbitexch_data = pair.orbitexch_data
            market_id = orbitexch_data.get("market_id", "")
            if market_id:
                orbitexch_pair_mapping[market_id] = pair_id

            # Selection mapping: pair_id -> {selection_id: market_type}
            if self._orbitexch_client:
                selection_mappings[pair_id] = self._orbitexch_client.get_selection_mapping(pair_id)

        return {
            "polymarket_pair_mapping": polymarket_pair_mapping,
            "orbitexch_pair_mapping": orbitexch_pair_mapping,
            "selection_mappings": selection_mappings,
        }

    def get_order_info(self, pair_id: str, market_type: str) -> dict | None:
        """
        获取创建订单所需的市场信息

        Args:
            pair_id: 比赛 ID
            market_type: 市场类型 (home/draw/away)

        Returns:
            {
                "polymarket": {
                    "token_id": str,
                    "condition_id": str,
                },
                "orbitexch": {
                    "market_id": str,
                    "selection_id": str,
                },
            }
            如果找不到返回 None
        """
        pair = self._subscribed_pairs.get(pair_id)
        if not pair:
            self._log.warning(f"get_order_info: Pair {pair_id} not found in subscribed pairs")
            return None

        result = {"polymarket": {}, "orbitexch": {}}

        # 从 Polymarket client 获取 token_id
        if self._polymarket_client:
            for token_id, token_info in self._polymarket_client._subscribed_tokens.items():
                if (token_info.get("event_id") == pair.polymarket_event_id and
                    token_info.get("market_type") == market_type):
                    result["polymarket"]["token_id"] = token_id
                    result["polymarket"]["condition_id"] = pair.polymarket_data.get("condition_id", "")
                    break

        # 从 OrbitExch data 获取 market_id 和 selection_id
        orbitexch_data = pair.orbitexch_data
        market_id = orbitexch_data.get("market_id", "")
        selection_id = orbitexch_data.get(f"{market_type}_selection_id", "")

        if market_id and selection_id:
            result["orbitexch"]["market_id"] = market_id
            result["orbitexch"]["selection_id"] = selection_id
        else:
            # 从 orbitexch_client 的 pair_info 获取
            if self._orbitexch_client:
                pair_info = self._orbitexch_client._pair_info.get(pair_id, {})
                result["orbitexch"]["market_id"] = pair_info.get("market_id", "")
                selections = pair_info.get("selections", {})
                result["orbitexch"]["selection_id"] = selections.get(market_type, "")

        # 记录缺失的数据以便调试
        poly_token = result["polymarket"].get("token_id", "")
        orbit_market = result["orbitexch"].get("market_id", "")
        orbit_selection = result["orbitexch"].get("selection_id", "")

        if not poly_token:
            self._log.warning(
                f"get_order_info: Missing Polymarket token_id for {pair_id}/{market_type}"
            )
        if not orbit_market or not orbit_selection:
            self._log.warning(
                f"get_order_info: Missing OrbitExch data for {pair_id}/{market_type}: "
                f"market_id={orbit_market}, selection_id={orbit_selection}"
            )

        return result

    async def refresh_all_positions_and_orders(self) -> None:
        """
        全量刷新 Polymarket 持仓和活跃订单的内存数据

        通过 REST API 查询最新数据，替换内存中的缓存。
        """
        if self._polymarket_client:
            # 刷新持仓
            await self._polymarket_client.fetch_positions()
            # 刷新活跃订单
            await self._polymarket_client.fetch_open_orders()
            self._log.info("Polymarket positions and orders refreshed")

        # OrbitExch 通过 WS CURRENT_BETS 自动维护，无需额外 REST 刷新

    def get_orbitexch_pages(self) -> dict:
        """
        获取 OrbitExch 的 Playwright 页面引用

        用于执行服务共享浏览器页面进行下单。

        Returns:
            {competition_id: Page}
        """
        if self._orbitexch_client:
            return self._orbitexch_client._pages
        return {}
