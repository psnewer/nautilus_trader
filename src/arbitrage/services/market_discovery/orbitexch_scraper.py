"""
OrbitExch 市场发现抓取器

功能：
使用 Playwright 模拟浏览器行为获取比赛信息
"""

import asyncio
import logging
from dataclasses import dataclass, field

try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout
except ImportError:
    raise ImportError(
        "Playwright is required. Install with: pip install playwright && playwright install chromium"
    )

from .config import OrbitExchVenueConfig, SportConfig


@dataclass
class MatchEvent:
    """比赛事件"""
    sport: str
    competition: str
    home_team: str
    away_team: str
    sport_id: str = ""
    competition_id: str = ""
    # OrbitExch market_id (唯一标识每场比赛)
    market_id: str = ""
    # OrbitExch selection IDs (用于赔率匹配，需要与 market_id 组合使用)
    home_selection_id: str = ""
    draw_selection_id: str = ""
    away_selection_id: str = ""


class OrbitExchScraper:
    """
    OrbitExch 抓取器

    使用 Playwright 模拟浏览器操作获取比赛信息。
    """

    BASE_URL = "https://www.orbitexch.com"

    def __init__(
        self,
        config: OrbitExchVenueConfig,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self._log = logger or logging.getLogger(self.__class__.__name__)

        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # =========================================================================
    # Playwright 生命周期
    # =========================================================================

    async def start_browser(self) -> None:
        """启动浏览器"""
        self._log.info("Starting Playwright browser for OrbitExch...")

        self._playwright = await async_playwright().start()

        # 使用 persistent context 如果配置了 user_data_dir
        if self.config.browser.user_data_dir:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=self.config.browser.user_data_dir,
                headless=self.config.browser.headless,
                channel="chrome",  # 使用系统 Chrome，避免 bundled Chromium 崩溃
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            )
            self._browser = self._context.browser
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        else:
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

        # 设置反检测脚本
        await self._setup_stealth()

        self._log.info("Browser started successfully")

    async def _setup_stealth(self) -> None:
        """设置反检测措施"""
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)

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
    # 导航和数据提取
    # =========================================================================

    async def navigate_to_homepage(self) -> None:
        """导航到首页"""
        if not self._page:
            await self.start_browser()

        self._log.info(f"Navigating to {self.BASE_URL}...")
        await self._page.goto(self.BASE_URL, wait_until="networkidle")
        await asyncio.sleep(2)  # 等待动态内容加载

    async def find_and_click_sport(self, sport_name: str) -> bool:
        """
        在左侧菜单中查找并点击 sport，然后点击 "All {Sport}" 展开所有 competitions

        Args:
            sport_name: sport 名称（如 "soccer"）

        Returns:
            是否成功点击
        """
        self._log.info(f"Looking for sport: {sport_name}")

        try:
            # 使用文本定位器查找 sport
            sport_locator = self._page.locator(f'text="{sport_name.title()}"').first
            if await sport_locator.count() > 0:
                await sport_locator.click()
                await asyncio.sleep(2)
                self._log.info(f"Clicked sport: {sport_name}")

                # 查找并点击 "All {Sport}" 以展开所有 competitions
                await self._click_all_sport(sport_name)
                return True

            # 备用：查找包含 sport 名称的元素
            sport_items = await self._page.query_selector_all('[data-sport-id]')
            for item in sport_items:
                text = await item.text_content()
                if text and sport_name.lower() in text.lower():
                    await item.click()
                    await asyncio.sleep(2)
                    self._log.info(f"Clicked sport: {sport_name}")

                    # 查找并点击 "All {Sport}"
                    await self._click_all_sport(sport_name)
                    return True

            self._log.warning(f"Sport {sport_name} not found")
            return False

        except Exception as e:
            self._log.error(f"Error finding sport {sport_name}: {e}")
            return False

    async def _click_all_sport(self, sport_name: str) -> bool:
        """
        点击 "All {Sport}" 条目以展开所有 competitions

        Args:
            sport_name: sport 名称

        Returns:
            是否成功点击
        """
        try:
            # 尝试多种格式：All Soccer, All Football 等
            all_sport_variants = [
                f'All {sport_name.title()}',
                f'All {sport_name}',
                f'All {sport_name.upper()}',
            ]

            for variant in all_sport_variants:
                all_sport_locator = self._page.locator(f'text="{variant}"').first
                if await all_sport_locator.count() > 0:
                    await all_sport_locator.click()
                    await asyncio.sleep(2)
                    self._log.info(f"Clicked '{variant}' to expand all competitions")
                    return True

            self._log.debug(f"'All {sport_name}' button not found, continuing...")
            return False

        except Exception as e:
            self._log.debug(f"Failed to click 'All {sport_name}': {e}")
            return False

    async def find_and_click_competition(self, competition_name: str) -> bool:
        """
        查找并点击 competition

        Args:
            competition_name: competition 名称

        Returns:
            是否成功点击
        """
        self._log.info(f"Looking for competition: {competition_name}")

        try:
            # 使用文本定位器查找 competition
            comp_locator = self._page.locator(f'text="{competition_name}"').first
            if await comp_locator.count() > 0:
                await comp_locator.click()
                await asyncio.sleep(3)
                self._log.info(f"Clicked competition: {competition_name}")
                return True

            # 备用：查找 datatype="competition" 的条目
            competition_items = await self._page.query_selector_all(
                '[datatype="competition"], [datatype="competiton"], [data-type="competition"]'
            )

            for item in competition_items:
                text = await item.text_content()
                if text and competition_name.lower() in text.lower():
                    await item.click()
                    await asyncio.sleep(3)
                    self._log.info(f"Clicked competition: {competition_name}")
                    return True

            self._log.warning(f"Competition {competition_name} not found")
            return False

        except Exception as e:
            self._log.error(f"Error finding competition {competition_name}: {e}")
            return False

    def _extract_ids_from_url(self, url: str) -> tuple[str, str]:
        """
        从 URL 中提取 sport_id 和 competition_id

        URL 格式: https://www.orbitexch.com/customer/sport/{sport_id}/competition/{competition_id}

        Args:
            url: 当前页面 URL

        Returns:
            (sport_id, competition_id) 元组
        """
        import re

        sport_id = ""
        competition_id = ""

        # 匹配 /sport/{sport_id}/competition/{competition_id}
        match = re.search(r'/sport/(\d+)/competition/(\d+)', url)
        if match:
            sport_id = match.group(1)
            competition_id = match.group(2)

        return sport_id, competition_id

    async def navigate_to_competition_page(
        self,
        sport_id: str,
        competition_id: str,
    ) -> bool:
        """
        导航到 competition 详情页

        Args:
            sport_id: sport ID
            competition_id: competition ID

        Returns:
            是否成功导航
        """
        url = f"{self.BASE_URL}/customer/sport/{sport_id}/competition/{competition_id}"
        self._log.info(f"Navigating to {url}...")

        try:
            await self._page.goto(url, wait_until="networkidle")
            await asyncio.sleep(2)
            return True
        except Exception as e:
            self._log.error(f"Failed to navigate to competition page: {e}")
            return False

    async def extract_matches(
        self,
        sport: str,
        competition: str,
        sport_id: str,
        competition_id: str,
    ) -> list[MatchEvent]:
        """
        从当前页面提取比赛信息

        规则：
        - 遍历 role="row" 的 div
        - 找到并列的两个 p 元素（主客队名）
        - 同时提取 data-selection-id（用于 WebSocket 匹配）

        Returns:
            比赛事件列表
        """
        self._log.info("Extracting matches from current page...")

        try:
            matches_data = await self._page.evaluate("""() => {
                const results = [];

                // 查找 role="row" 的元素
                const rows = document.querySelectorAll('[role="row"]');

                rows.forEach(row => {
                    // 查找并列的两个 p 元素（队名）
                    const pElements = row.querySelectorAll('p');

                    if (pElements.length >= 2) {
                        const homeTeam = pElements[0].textContent?.trim() || '';
                        const awayTeam = pElements[1].textContent?.trim() || '';

                        if (homeTeam && awayTeam) {
                            // 提取 data-selection-id 和 data-market-id
                            // 通常有 3 个选项：主胜、平局、客胜
                            const selectionElements = row.querySelectorAll('[data-selection-id]');
                            let marketId = '';
                            let homeSelectionId = '';
                            let drawSelectionId = '';
                            let awaySelectionId = '';

                            // 获取 market_id（从第一个 selection 元素向上查找）
                            if (selectionElements.length > 0) {
                                const firstSel = selectionElements[0];
                                marketId = firstSel.closest('[data-market-id]')?.getAttribute('data-market-id') || '';
                            }

                            // 按顺序：第1个=主胜，第2个=平局，第3个=客胜
                            if (selectionElements.length >= 3) {
                                homeSelectionId = selectionElements[0].getAttribute('data-selection-id') || '';
                                drawSelectionId = selectionElements[1].getAttribute('data-selection-id') || '';
                                awaySelectionId = selectionElements[2].getAttribute('data-selection-id') || '';
                            } else if (selectionElements.length === 2) {
                                // Tennis 等只有两个选项
                                homeSelectionId = selectionElements[0].getAttribute('data-selection-id') || '';
                                awaySelectionId = selectionElements[1].getAttribute('data-selection-id') || '';
                            }

                            results.push({
                                home_team: homeTeam,
                                away_team: awayTeam,
                                market_id: marketId,
                                home_selection_id: homeSelectionId,
                                draw_selection_id: drawSelectionId,
                                away_selection_id: awaySelectionId
                            });
                        }
                    }
                });

                return results;
            }""")

            events = [
                MatchEvent(
                    sport=sport,
                    competition=competition,
                    home_team=m["home_team"],
                    away_team=m["away_team"],
                    sport_id=sport_id,
                    competition_id=competition_id,
                    market_id=m.get("market_id", ""),
                    home_selection_id=m.get("home_selection_id", ""),
                    draw_selection_id=m.get("draw_selection_id", ""),
                    away_selection_id=m.get("away_selection_id", ""),
                )
                for m in matches_data
                if m["home_team"] and m["away_team"]
            ]

            self._log.info(f"Extracted {len(events)} matches with market_id and selection IDs")
            for e in events:
                self._log.debug(
                    f"  {e.home_team} vs {e.away_team}: market={e.market_id}, "
                    f"home={e.home_selection_id}, draw={e.draw_selection_id}, away={e.away_selection_id}"
                )

            return events

        except Exception as e:
            self._log.error(f"Error extracting matches: {e}")
            return []

    async def go_back(self) -> None:
        """返回上一页"""
        try:
            await self._page.go_back(wait_until="networkidle")
            await asyncio.sleep(1)
        except Exception as e:
            self._log.warning(f"Failed to go back: {e}")

    # =========================================================================
    # 主流程
    # =========================================================================

    async def discover_events(
        self,
        sport_configs: list[SportConfig] | None = None,
    ) -> list[MatchEvent]:
        """
        发现比赛事件

        Args:
            sport_configs: 要抓取的 sport 和 competition 配置列表
                          如果为 None，使用 self.config.sports

        Returns:
            发现的比赛事件列表
        """
        if sport_configs is None:
            sport_configs = self.config.sports

        if not sport_configs:
            self._log.warning("No sport configs provided")
            return []

        if not self._page:
            await self.start_browser()

        all_events: list[MatchEvent] = []

        for sport_config in sport_configs:
            sport_name = sport_config.sport

            # 遍历 competitions，每个都从首页开始
            for competition_name in sport_config.competitions:
                self._log.info(f"Processing {sport_name} > {competition_name}")

                # 每次都从首页开始，确保菜单状态正确
                await self.navigate_to_homepage()

                # 点击 sport 展开
                if not await self.find_and_click_sport(sport_name):
                    continue

                # 点击 competition
                if not await self.find_and_click_competition(competition_name):
                    continue

                # 从当前 URL 提取 sport_id 和 competition_id
                current_url = self._page.url
                sport_id, competition_id = self._extract_ids_from_url(current_url)
                self._log.info(
                    f"Extracted IDs from URL: sport_id='{sport_id}', "
                    f"competition_id='{competition_id}' (URL: {current_url})"
                )

                # 提取比赛信息
                events = await self.extract_matches(
                    sport=sport_name,
                    competition=competition_name,
                    sport_id=sport_id,
                    competition_id=competition_id,
                )
                all_events.extend(events)

        self._log.info(f"Discovered {len(all_events)} total match events from OrbitExch")
        return all_events
