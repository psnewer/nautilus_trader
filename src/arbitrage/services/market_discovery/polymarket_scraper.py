"""
Polymarket 市场发现抓取器

功能：
1. 从 polymarket.com/sports 抓取 sport-competition 映射
2. 从 Gamma API 获取 events 并解析队名
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
except ImportError:
    raise ImportError(
        "Playwright is required. Install with: pip install playwright && playwright install chromium"
    )

from .config import PolymarketVenueConfig


@dataclass
class SportCompetitionMapping:
    """Sport-Competition 映射"""
    sport: str
    competition: str


@dataclass
class MatchEvent:
    """比赛事件"""
    sport: str
    competition: str
    home_team: str
    away_team: str
    event_id: str = ""
    title: str = ""
    tags: list[str] = field(default_factory=list)


class PolymarketScraper:
    """
    Polymarket 抓取器

    使用 Playwright 获取 sport-competition 映射，
    使用 Gamma API 获取事件数据。
    """

    SPORTS_PAGE_URL = "https://polymarket.com/sports"

    def __init__(
        self,
        config: PolymarketVenueConfig,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self._log = logger or logging.getLogger(self.__class__.__name__)

        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

        # sport-competition 映射缓存
        self._mapping_cache: list[SportCompetitionMapping] = []

    # =========================================================================
    # Playwright 生命周期
    # =========================================================================

    async def start_browser(self) -> None:
        """启动浏览器"""
        self._log.info("Starting Playwright browser for Polymarket...")

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.browser.headless,
            channel="chrome",  # 使用系统 Chrome，避免 bundled Chromium 崩溃
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self.config.browser.timeout_ms)

        self._log.info("Browser started successfully")

    async def close_browser(self) -> None:
        """关闭浏览器"""
        self._log.info("Closing Playwright browser...")

        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

        self._log.info("Browser closed")

    # =========================================================================
    # Sport-Competition 映射抓取
    # =========================================================================

    async def scrape_sport_competition_mapping(self) -> list[SportCompetitionMapping]:
        """
        从 polymarket.com/sports 抓取 sport-competition 映射

        规则（来自 requirements.md）：
        - group/sports-item 的 div 下有两个 div
        - 第一个 div 的属性值为 sport
        - 第二个 div 的属性值为 competition

        Returns:
            sport-competition 映射列表
        """
        if not self._page:
            await self.start_browser()

        self._log.info(f"Navigating to {self.SPORTS_PAGE_URL}...")

        try:
            await self._page.goto(self.SPORTS_PAGE_URL, wait_until="networkidle")
            await asyncio.sleep(2)  # 等待动态内容加载

            # 使用 JavaScript 提取映射
            mappings = await self._page.evaluate("""() => {
                const results = [];

                // 查找 sports-item 容器
                const sportsItems = document.querySelectorAll('[class*="sports-item"], [class*="group"]');

                sportsItems.forEach(item => {
                    const divs = item.querySelectorAll(':scope > div');

                    if (divs.length >= 2) {
                        const sport = divs[0].textContent?.trim() || '';
                        const competition = divs[1].textContent?.trim() || '';

                        if (sport && competition) {
                            results.push({ sport, competition });
                        }
                    }
                });

                // 备用方案：查找具有特定结构的元素
                if (results.length === 0) {
                    document.querySelectorAll('a[href*="/sports/"]').forEach(link => {
                        const href = link.getAttribute('href') || '';
                        const match = href.match(/\\/sports\\/([^/]+)\\/([^/]+)/);
                        if (match) {
                            results.push({
                                sport: match[1].replace(/-/g, ' '),
                                competition: match[2].replace(/-/g, ' ')
                            });
                        }
                    });
                }

                return results;
            }""")

            self._mapping_cache = [
                SportCompetitionMapping(sport=m["sport"], competition=m["competition"])
                for m in mappings
            ]

            self._log.info(f"Found {len(self._mapping_cache)} sport-competition mappings")
            return self._mapping_cache

        except Exception as e:
            self._log.error(f"Failed to scrape sport-competition mapping: {e}")
            return []

    def get_sport_for_competition(self, competition: str) -> str | None:
        """根据 competition 查找对应的 sport"""
        from src.arbitrage.common.utils import select_best_match

        competition_list = [m.competition for m in self._mapping_cache]
        matched = select_best_match(competition_list, competition)

        if matched:
            for m in self._mapping_cache:
                if m.competition == matched:
                    return m.sport

        return None

    # =========================================================================
    # Gamma API 事件抓取
    # =========================================================================

    async def fetch_sports_tags(self) -> list[dict[str, Any]]:
        """
        获取 Gamma API 的 sports 数据

        Returns:
            sports 数据列表
        """
        url = f"{self.config.api.base_url}{self.config.api.sports_endpoint}"
        self._log.info(f"Fetching sports from {url}...")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    async def fetch_events_by_tag(self, tag_id: str) -> list[dict[str, Any]]:
        """
        根据 tag_id 获取 events

        Args:
            tag_id: tag ID

        Returns:
            events 列表
        """
        url = f"{self.config.api.base_url}{self.config.api.events_endpoint}"
        params = {
            "tag_id": tag_id,
            "closed": str(self.config.api.closed).lower(),
            "limit": self.config.api.events_limit,
        }

        self._log.debug(f"Fetching events for tag {tag_id}...")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    async def fetch_series_events(self, series_id: str) -> list[dict[str, Any]]:
        """
        根据 series_id 获取 events

        Args:
            series_id: series ID

        Returns:
            events 列表
        """
        url = f"{self.config.api.base_url}/series/{series_id}"
        # 添加 closed 参数过滤（只取 closed=false 的 events）
        params = {
            "closed": "false",
        }
        self._log.debug(f"Fetching series {series_id} (closed=false)...")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            # series API 返回 {"events": [...], ...}
            return data.get("events", [])

    def _count_tag_occurrences(self, sports_data: list[dict]) -> dict[str, int]:
        """统计每个 tag 在 sports_data 中出现的次数"""
        tag_counts: dict[str, int] = {}

        for sport in sports_data:
            tags = sport.get("tags", [])
            for tag in tags:
                # tag 可能是 dict 或 str
                if isinstance(tag, dict):
                    tag_id = tag.get("id") or tag.get("tag_id") or str(tag)
                else:
                    tag_id = str(tag)
                tag_counts[tag_id] = tag_counts.get(tag_id, 0) + 1

        return tag_counts

    def _parse_team_names(self, title: str) -> tuple[str, str] | None:
        """
        解析 event title 获取主队和客队名称

        规则（来自 requirements.md）：
        1. 如果 title 含 "vs."，按 "vs." 拆分
        2. 否则按 "vs" 拆分
        3. 第一个为主队，第二个为客队
        4. 使用 : 或 - 清洗队名

        Returns:
            (home_team, away_team) 或 None（如果无法解析）
        """
        # 预处理：去掉常见前缀
        cleaned_title = title
        prefixes_to_remove = [
            "Who will win ",
            "Who will win the ",
            "draft ",
        ]
        for prefix in prefixes_to_remove:
            if cleaned_title.startswith(prefix):
                cleaned_title = cleaned_title[len(prefix):]
                break

        # 预处理：去掉 ", scheduled for..." 及之后的内容
        if ", scheduled for" in cleaned_title:
            cleaned_title = cleaned_title.split(", scheduled for")[0]

        # 预处理：去掉 "?" 结尾
        cleaned_title = cleaned_title.rstrip("?").strip()

        # 尝试多种 vs 格式拆分
        # 优先级：vs. > " vs " > "vs"（无空格）
        if "vs." in cleaned_title:
            parts = cleaned_title.split("vs.", 1)
        elif " vs " in cleaned_title:
            parts = cleaned_title.split(" vs ", 1)
        elif "vs" in cleaned_title.lower():
            # 使用正则处理 vs 无空格情况（大小写不敏感）
            match = re.split(r'\bvs\b', cleaned_title, maxsplit=1, flags=re.IGNORECASE)
            if len(match) == 2:
                parts = match
            else:
                return None
        else:
            return None

        if len(parts) != 2:
            return None

        home_raw = parts[0].strip()
        away_raw = parts[1].strip()

        # 清洗队名：去掉 : 或 - 前/后的内容
        # 主队：去掉第一个 : 或 - 之前的字符串
        # 客队：去掉第一个 : 或 - 之后的字符串
        home_team = self._clean_team_name(home_raw, keep_after=True)
        away_team = self._clean_team_name(away_raw, keep_after=False)

        return (home_team, away_team)

    def _clean_team_name(self, name: str, keep_after: bool) -> str:
        """
        清洗队名

        Args:
            name: 原始队名
            keep_after: True 保留分隔符之后部分，False 保留之前部分
        """
        # 查找 : 或 - 的位置
        colon_pos = name.find(":")
        dash_pos = name.find("-")

        # 选择第一个出现的分隔符
        sep_pos = -1
        if colon_pos >= 0 and dash_pos >= 0:
            sep_pos = min(colon_pos, dash_pos)
        elif colon_pos >= 0:
            sep_pos = colon_pos
        elif dash_pos >= 0:
            sep_pos = dash_pos

        if sep_pos >= 0:
            if keep_after:
                return name[sep_pos + 1:].strip()
            else:
                return name[:sep_pos].strip()

        return name.strip()

    async def discover_events(
        self,
        target_sports: list[str] | None = None,
        target_competitions: list[str] | None = None,
    ) -> list[MatchEvent]:
        """
        发现比赛事件

        使用 /sports 获取 series ID，然后从 /series/{id} 获取比赛事件。

        注意：API 返回的 "sport" 字段实际上是 competition 的缩写（如 epl, lal）

        Args:
            target_sports: 目标 sport 列表（用于最终赋值）
            target_competitions: 目标 competition 列表（用于过滤 API 返回的数据）

        Returns:
            发现的比赛事件列表
        """
        # 获取 sports 数据（包含 series ID）
        # 注意：API 返回的 "sport" 字段实际是 competition 缩写
        sports_data = await self.fetch_sports_tags()
        self._log.info(f"Fetched {len(sports_data)} competitions from API")

        # 从配置构建 competition -> sport 映射
        competition_to_sport: dict[str, str] = {}
        if self.config.sports:
            for sport_config in self.config.sports:
                for comp in sport_config.competitions:
                    # 支持大小写不敏感的匹配
                    competition_to_sport[comp.lower()] = sport_config.sport

        self._log.debug(f"Competition-to-sport mapping: {competition_to_sport}")

        events: list[MatchEvent] = []
        processed_events: set[str] = set()

        # 遍历每个 competition，获取其 series 中的比赛
        for comp_info in sports_data:
            competition_name = comp_info.get("sport", "")  # API 字段名是 sport，但实际是 competition
            series_id = comp_info.get("series")

            self._log.debug(f"Processing competition: {competition_name} (series_id={series_id})")

            if not series_id:
                self._log.debug(f"Skipping {competition_name}: no series_id")
                continue

            # 过滤 competition
            if target_competitions:
                # 大小写不敏感匹配
                comp_match = any(
                    tc.lower() == competition_name.lower()
                    for tc in target_competitions
                )
                if not comp_match:
                    self._log.debug(f"Skipping {competition_name}: not in target_competitions {target_competitions}")
                    continue

            # 查找对应的 sport
            sport_name = competition_to_sport.get(competition_name.lower())
            if not sport_name:
                self._log.warning(f"Competition {competition_name} not found in config, skipping")
                continue

            self._log.info(f"Matched competition: {competition_name} -> sport: {sport_name}, fetching series {series_id}...")

            self._log.debug(f"Fetching series {series_id} for sport {sport_name}...")

            try:
                series_events = await self.fetch_series_events(str(series_id))
                self._log.debug(f"Series {series_id} returned {len(series_events)} events")

                for event_data in series_events:
                    event_id = str(event_data.get("id", ""))
                    is_closed = event_data.get("closed", False)
                    is_active = event_data.get("active", False)
                    title = event_data.get("title", "")

                    if event_id in processed_events:
                        continue

                    # 只取 closed=false 且 active=true 的事件
                    if is_closed:
                        self._log.debug(f"Skipping closed event {event_id}: {title}")
                        continue

                    if not is_active:
                        self._log.debug(f"Skipping inactive event {event_id}: {title}")
                        continue

                    # 跳过 "More Markets" 类型的事件
                    if "More Markets" in title:
                        continue

                    # 跳过 draft 事件
                    if title.lower().startswith("draft "):
                        continue

                    team_names = self._parse_team_names(title)

                    if not team_names:
                        continue  # 跳过无法解析的事件

                    home_team, away_team = team_names

                    # 直接使用原始 competition 名称（如 "epl"）
                    # matching 阶段会通过 competition_aliases 标准化
                    match_event = MatchEvent(
                        sport=sport_name,
                        competition=competition_name,
                        home_team=home_team,
                        away_team=away_team,
                        event_id=event_id,
                        title=title,
                        tags=[],
                    )

                    events.append(match_event)
                    processed_events.add(event_id)

                    self._log.debug(
                        f"Found event: {home_team} vs {away_team} "
                        f"({competition_name}, {sport_name})"
                    )

            except Exception as e:
                self._log.warning(f"Failed to fetch series {series_id}: {e}")
                continue

        self._log.info(f"Discovered {len(events)} match events from Polymarket")
        return events

    def _find_competition(
        self,
        tags: list,
        series: dict | None,
    ) -> str | None:
        """
        查找 competition

        规则：
        1. 先从 tags 中找在 mapping 缓存中出现的
        2. 如果没找到，从 series.title 获取
        3. 如果没有 series，返回 None

        Args:
            tags: event 的 tags
            series: event 的 series 信息

        Returns:
            competition 名称或 None
        """
        from src.arbitrage.common.utils import select_best_match

        # 从 tags 查找
        known_competitions = [m.competition for m in self._mapping_cache]
        for tag in tags:
            # tag 可能是 dict 或 str
            if isinstance(tag, dict):
                tag_name = tag.get("name") or tag.get("label") or str(tag)
            else:
                tag_name = str(tag)
            matched = select_best_match(known_competitions, tag_name)
            if matched:
                return matched

        # 从 series 获取
        if series:
            return series.get("title")

        return None
