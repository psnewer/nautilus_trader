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
from .messages import OddsUpdateMessage, MatchStatusMessage
from .orbitexch_client import OrbitExchOddsClient
from .polymarket_client import PolymarketOddsClient
from .topics import odds_topic, match_status_topic


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
        self._msgbus = msgbus

        # 客户端
        self._polymarket_client: PolymarketOddsClient | None = None
        self._orbitexch_client: OrbitExchOddsClient | None = None

        # 订阅状态
        self._subscribed_pairs: dict[str, MatchedPair] = {}  # pair_id -> MatchedPair
        self._latest_odds: dict[str, dict] = {}  # pair_id -> {polymarket: {...}, orbitexch: {...}}

        # 超时监控
        self._last_updates: dict[str, float] = {}  # pair_id -> timestamp
        self._heartbeat_task: asyncio.Task | None = None

        # 运行状态
        self._running = False

        # 仓位变化回调
        self._position_callbacks: list[Callable[[str], None]] = []

    # =========================================================================
    # 生命周期
    # =========================================================================

    async def start(self) -> None:
        """启动服务"""
        if self._running:
            self._log.warning("Service already running")
            return

        self._log.info("Starting odds subscription service...")

        # 创建客户端
        self._polymarket_client = PolymarketOddsClient(
            config=self.config,
            logger=logging.getLogger("PolymarketOdds"),
        )

        self._orbitexch_client = OrbitExchOddsClient(
            config=self.config,
            logger=logging.getLogger("OrbitExchOdds"),
        )

        # 设置回调
        self._polymarket_client.on_price_update(self._on_polymarket_update)
        self._orbitexch_client.on_price_update(self._on_orbitexch_update)
        self._orbitexch_client.register_status_callback(self._on_match_status_update)

        # 设置仓位变化回调
        self._orbitexch_client.register_bets_callback(self._on_orbitexch_bets_update)
        self._polymarket_client.register_positions_callback(self._on_polymarket_position_update)

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
        self._log.info(
            f"Odds config updated: staleness_timeout={config.staleness_timeout_sec}s, "
            f"orbitexch_staleness_timeout={config.orbitexch_staleness_timeout_sec}s"
        )

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
        self._last_updates[pair_id] = time.time()

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
                if pair_id not in self._latest_odds:
                    self._latest_odds[pair_id] = {"polymarket": {}, "orbitexch": {}}

                self._latest_odds[pair_id]["polymarket"][market_type] = odds_data

                # 更新时间戳
                self._last_updates[pair_id] = time.time()

                self._log.debug(
                    f"Polymarket update: {pair_id} {market_type} "
                    f"bid={odds_data.get('bid')} ask={odds_data.get('ask')}"
                )

                # 发布赔率更新
                self._publish_odds_update(pair_id, "polymarket", odds_data)
                break

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
            if pair_id not in self._latest_odds:
                self._latest_odds[pair_id] = {"polymarket": {}, "orbitexch": {}}

            self._latest_odds[pair_id]["orbitexch"][market_type] = {
                "market_type": market_type,
                "back": back,
                "lay": lay,
                "timestamp": odds_data.get("timestamp"),
            }

            # 更新时间戳
            self._last_updates[pair_id] = time.time()

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

    def _publish_odds_update(self, pair_id: str, venue: str, odds_data: dict) -> None:
        """发布赔率更新"""
        if not self._msgbus:
            return
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

    def _on_match_status_update(self, pair_id: str, is_live: bool) -> None:
        """发布比赛状态更新"""
        if not self._msgbus:
            return
        msg = MatchStatusMessage(pair_id=pair_id, is_live=is_live, source="orbitexch")
        self._msgbus.publish(match_status_topic(pair_id), msg, external_pub=False)

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

    def _on_orbitexch_bets_update(self, bets_data: dict) -> None:
        """
        OrbitExch 订单更新回调

        Args:
            bets_data: {"market_id": str, "bets": list}
        """
        market_id = bets_data.get("market_id", "")

        # 查找对应的 pair_id
        for pair_id, pair in self._subscribed_pairs.items():
            orbitexch_data = pair.orbitexch_data
            if orbitexch_data.get("market_id") == market_id:
                self._log.debug(f"OrbitExch bets update for {pair_id}")
                self._notify_position_callbacks(pair_id)
                break

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
        """检查数据新鲜度"""
        now = time.time()
        timeout_sec = self.config.staleness_timeout_sec

        for pair_id, last_update in list(self._last_updates.items()):
            if now - last_update > timeout_sec:
                self._log.warning(
                    f"Stale data detected for pair {pair_id}: "
                    f"{int(now - last_update)}s since last update"
                )

                # 触发刷新
                await self._refresh_pair(pair_id)

    async def _refresh_pair(self, pair_id: str) -> None:
        """
        刷新 pair 的订阅

        Args:
            pair_id: pair ID
        """
        pair = self._subscribed_pairs.get(pair_id)
        if not pair:
            return

        self._log.info(f"Refreshing pair {pair_id}")

        # Polymarket: 重新订阅
        if pair.polymarket_event_id and self._polymarket_client:
            await self._polymarket_client.subscribe_event(pair.polymarket_event_id)

        # OrbitExch: 刷新页面（不关闭浏览器）
        if self._orbitexch_client:
            await self._orbitexch_client.refresh_page()

        # 重置时间戳
        self._last_updates[pair_id] = time.time()

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

    def get_subscriptions(self) -> list[dict[str, Any]]:
        """
        获取订阅状态

        Returns:
            订阅列表
        """
        subscriptions = []

        for pair_id, pair in self._subscribed_pairs.items():
            last_update = self._last_updates.get(pair_id, 0)
            age_sec = int(time.time() - last_update) if last_update > 0 else None

            subscriptions.append({
                "pair_id": pair_id,
                "sport": pair.sport,
                "competition": pair.competition,
                "polymarket_event_id": pair.polymarket_event_id,
                "last_update_sec_ago": age_sec,
                "is_stale": age_sec > self.config.staleness_timeout_sec if age_sec else False,
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
