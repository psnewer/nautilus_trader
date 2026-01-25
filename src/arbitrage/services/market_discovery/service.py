"""
市场发现服务

职责：
- 发现并监控各交易所可交易市场
- 发布 MarketDiscoveredEvent 事件
- 订阅 ConfigUpdatedEvent 更新配置
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .config import MarketDiscoveryConfig, SportConfig
from .polymarket_scraper import PolymarketScraper, MatchEvent as PolymarketMatch
from .orbitexch_scraper import OrbitExchScraper, MatchEvent as OrbitExchMatch


@dataclass
class MarketDiscoveredEvent:
    """
    市场发现事件

    当发现新市场或市场更新时发布此事件
    """
    venue_id: str              # 交易所 ID (POLYMARKET/ORBITEXCH)
    sport: str                 # sport 名称
    competition: str           # competition 名称
    home_team: str             # 主队
    away_team: str             # 客队
    event_id: str              # 事件 ID
    action: str                # ADDED/UPDATED/REMOVED
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)


class DiscoveryService:
    """
    市场发现服务

    负责从多个平台发现比赛市场并发布事件。

    使用示例:
    ```python
    config = MarketDiscoveryConfig.from_dict({
        "enabled": True,
        "venues": {
            "polymarket": {
                "enabled": True,
                "sports": [{"sport": "soccer", "competitions": ["EPL"]}]
            }
        }
    })

    service = DiscoveryService(config)

    # 注册事件处理器
    service.on_market_discovered(lambda event: print(event))

    # 运行一次发现
    await service.discover_all()

    # 或启动定时轮询
    await service.start()
    ```
    """

    def __init__(
        self,
        config: MarketDiscoveryConfig,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self._log = logger or logging.getLogger(self.__class__.__name__)

        # 抓取器实例
        self._polymarket_scraper: PolymarketScraper | None = None
        self._orbitexch_scraper: OrbitExchScraper | None = None

        # 事件处理器
        self._event_handlers: list[Callable[[MarketDiscoveredEvent], None]] = []

        # 已发现的市场缓存（用于检测变更）
        self._discovered_markets: dict[str, MarketDiscoveredEvent] = {}

        # 运行状态
        self._running = False
        self._poll_task: asyncio.Task | None = None

    # =========================================================================
    # 生命周期
    # =========================================================================

    async def start(self) -> None:
        """启动服务（开始定时轮询）"""
        if not self.config.enabled:
            self._log.warning("DiscoveryService is disabled")
            return

        self._log.info("Starting DiscoveryService...")
        self._running = True

        # 初始化抓取器
        await self._init_scrapers()

        # 启动定时轮询任务
        self._poll_task = asyncio.create_task(self._poll_loop())

        self._log.info("DiscoveryService started")

    async def stop(self) -> None:
        """停止服务"""
        self._log.info("Stopping DiscoveryService...")
        self._running = False

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        # 关闭抓取器
        await self._close_scrapers()

        self._log.info("DiscoveryService stopped")

    async def _init_scrapers(self) -> None:
        """初始化抓取器"""
        if self.config.venues.polymarket.enabled:
            self._polymarket_scraper = PolymarketScraper(
                config=self.config.venues.polymarket,
                logger=self._log,
            )
            await self._polymarket_scraper.start_browser()

        if self.config.venues.orbitexch.enabled:
            self._orbitexch_scraper = OrbitExchScraper(
                config=self.config.venues.orbitexch,
                logger=self._log,
            )
            await self._orbitexch_scraper.start_browser()

    async def _close_scrapers(self) -> None:
        """关闭抓取器"""
        if self._polymarket_scraper:
            await self._polymarket_scraper.close_browser()
            self._polymarket_scraper = None

        if self._orbitexch_scraper:
            await self._orbitexch_scraper.close_browser()
            self._orbitexch_scraper = None

    async def _poll_loop(self) -> None:
        """定时轮询循环"""
        while self._running:
            try:
                await self.discover_all()
            except Exception as e:
                self._log.error(f"Error in discovery poll: {e}")

            await asyncio.sleep(self.config.poll_interval_sec)

    # =========================================================================
    # 事件处理
    # =========================================================================

    def on_market_discovered(
        self,
        handler: Callable[[MarketDiscoveredEvent], None],
    ) -> None:
        """
        注册市场发现事件处理器

        Args:
            handler: 事件处理函数
        """
        self._event_handlers.append(handler)

    def _publish_event(self, event: MarketDiscoveredEvent) -> None:
        """发布事件到所有处理器"""
        for handler in self._event_handlers:
            try:
                handler(event)
            except Exception as e:
                self._log.error(f"Error in event handler: {e}")

    def _generate_market_key(
        self,
        venue_id: str,
        sport: str,
        competition: str,
        home_team: str,
        away_team: str,
    ) -> str:
        """生成市场唯一键"""
        return f"{venue_id}:{sport}:{competition}:{home_team}:{away_team}"

    def _process_discovered_match(
        self,
        venue_id: str,
        sport: str,
        competition: str,
        home_team: str,
        away_team: str,
        event_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """
        处理发现的比赛

        检测是新增还是更新，并发布相应事件
        """
        market_key = self._generate_market_key(
            venue_id, sport, competition, home_team, away_team
        )

        if market_key in self._discovered_markets:
            # 已存在，检查是否有变化
            existing = self._discovered_markets[market_key]
            # 简单起见，不检测更新，只记录首次发现
            return

        # 新发现的市场
        event = MarketDiscoveredEvent(
            venue_id=venue_id,
            sport=sport,
            competition=competition,
            home_team=home_team,
            away_team=away_team,
            event_id=event_id,
            action="ADDED",
            metadata=metadata or {},
        )

        self._discovered_markets[market_key] = event
        self._publish_event(event)

        self._log.info(
            f"Discovered: {venue_id} - {home_team} vs {away_team} "
            f"({competition}, {sport})"
        )

    # =========================================================================
    # 发现逻辑
    # =========================================================================

    async def discover_all(self) -> list[MarketDiscoveredEvent]:
        """
        执行一次完整的市场发现

        Returns:
            新发现的市场事件列表
        """
        self._log.info("Starting market discovery...")
        initial_count = len(self._discovered_markets)

        # Polymarket 发现
        if self._polymarket_scraper:
            await self._discover_polymarket()

        # OrbitExch 发现
        if self._orbitexch_scraper:
            await self._discover_orbitexch()

        new_count = len(self._discovered_markets) - initial_count
        self._log.info(f"Discovery complete. Found {new_count} new markets")

        return list(self._discovered_markets.values())

    async def _discover_polymarket(self) -> None:
        """从 Polymarket 发现市场"""
        self._log.info("Discovering markets from Polymarket...")

        # 获取配置的 sports 和 competitions
        sport_configs = self.config.venues.polymarket.sports
        target_sports = [s.sport for s in sport_configs]
        target_competitions = []
        for s in sport_configs:
            target_competitions.extend(s.competitions)

        try:
            events = await self._polymarket_scraper.discover_events(
                target_sports=target_sports if target_sports else None,
                target_competitions=target_competitions if target_competitions else None,
            )

            for event in events:
                self._process_discovered_match(
                    venue_id="POLYMARKET",
                    sport=event.sport,
                    competition=event.competition,
                    home_team=event.home_team,
                    away_team=event.away_team,
                    event_id=event.event_id,
                    metadata={"title": event.title, "tags": event.tags},
                )

        except Exception as e:
            self._log.error(f"Polymarket discovery failed: {e}")

    async def _discover_orbitexch(self) -> None:
        """从 OrbitExch 发现市场"""
        self._log.info("Discovering markets from OrbitExch...")

        sport_configs = self.config.venues.orbitexch.sports

        try:
            events = await self._orbitexch_scraper.discover_events(
                sport_configs=sport_configs if sport_configs else None,
            )

            for event in events:
                self._process_discovered_match(
                    venue_id="ORBITEXCH",
                    sport=event.sport,
                    competition=event.competition,
                    home_team=event.home_team,
                    away_team=event.away_team,
                    metadata={
                        "sport_id": event.sport_id,
                        "competition_id": event.competition_id,
                    },
                )

        except Exception as e:
            self._log.error(f"OrbitExch discovery failed: {e}")

    # =========================================================================
    # 配置更新
    # =========================================================================

    async def update_config(self, new_config: MarketDiscoveryConfig) -> None:
        """
        更新配置

        Args:
            new_config: 新配置
        """
        self._log.info("Updating DiscoveryService config...")

        old_enabled = self.config.enabled
        self.config = new_config

        # 如果启用状态变化，需要重启服务
        if old_enabled != new_config.enabled:
            if new_config.enabled:
                await self.start()
            else:
                await self.stop()

        # 重新初始化抓取器（配置可能变化）
        if self._running:
            await self._close_scrapers()
            await self._init_scrapers()

        self._log.info("Config updated successfully")

    # =========================================================================
    # 查询接口
    # =========================================================================

    def get_active_markets(self) -> dict[str, list[MarketDiscoveredEvent]]:
        """
        获取所有活跃市场

        Returns:
            按 venue_id 分组的市场列表
        """
        result: dict[str, list[MarketDiscoveredEvent]] = {}

        for event in self._discovered_markets.values():
            if event.venue_id not in result:
                result[event.venue_id] = []
            result[event.venue_id].append(event)

        return result

    def get_markets_by_venue(self, venue_id: str) -> list[MarketDiscoveredEvent]:
        """
        获取特定交易所的市场

        Args:
            venue_id: 交易所 ID

        Returns:
            市场列表
        """
        return [
            e for e in self._discovered_markets.values()
            if e.venue_id == venue_id
        ]

    def get_markets_by_competition(
        self,
        competition: str,
    ) -> list[MarketDiscoveredEvent]:
        """
        获取特定 competition 的市场

        Args:
            competition: competition 名称

        Returns:
            市场列表
        """
        from src.arbitrage.common.utils import get_similar_enhanced

        result = []
        for event in self._discovered_markets.values():
            # 精确匹配或模糊匹配
            if event.competition == competition:
                result.append(event)
            else:
                _, _, is_match = get_similar_enhanced(event.competition, competition)
                if is_match:
                    result.append(event)

        return result
