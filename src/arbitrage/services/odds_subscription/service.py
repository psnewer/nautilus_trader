"""
赔率订阅服务

协调 Polymarket 和 OrbitExch 客户端，管理赔率订阅生命周期。
"""

import asyncio
import logging
import time
from typing import Any

from src.arbitrage.services.market_matching.service import MatchedPair

from .config import OddsSubscriptionConfig
from .polymarket_client import PolymarketOddsClient
from .orbitexch_client import OrbitExchOddsClient


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
    ):
        self.config = config or OddsSubscriptionConfig()
        self._log = logger or logging.getLogger(self.__class__.__name__)

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

    # =========================================================================
    # 订阅管理
    # =========================================================================

    async def subscribe_matched_pairs(self, matched_pairs: list[MatchedPair]) -> None:
        """
        订阅 matched pairs 的赔率（并行处理）

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
            # 注册 selection_id -> (pair_id, market_type) 映射
            home_sel_id = orbitexch_data.get("home_selection_id", "")
            draw_sel_id = orbitexch_data.get("draw_selection_id", "")
            away_sel_id = orbitexch_data.get("away_selection_id", "")

            if home_sel_id:
                self._orbitexch_client.register_selection(home_sel_id, pair_id, "home")
            if draw_sel_id:
                self._orbitexch_client.register_selection(draw_sel_id, pair_id, "draw")
            if away_sel_id:
                self._orbitexch_client.register_selection(away_sel_id, pair_id, "away")

            self._log.info(
                f"Registered OrbitExch selections for {pair_id}: "
                f"home={home_sel_id}, draw={draw_sel_id}, away={away_sel_id}"
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
