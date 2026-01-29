"""
OrbitExch 赔率客户端

使用 Playwright 抓取 OrbitExch 赔率数据。

关键特性：
- 浏览器长期打开，持续监听数据更新
- 支持页面缩放以显示更多比赛
- 通过页面 JavaScript 监听赔率变化
"""

import asyncio
import logging
from typing import Any, Callable
from playwright.async_api import async_playwright, Page, Browser


from .config import OddsSubscriptionConfig


class OrbitExchOddsClient:
    """
    OrbitExch 赔率客户端

    功能：
    1. 打开浏览器并登录 OrbitExch
    2. 导航到指定 competition 页面
    3. 设置页面缩放
    4. 持续监听页面上的赔率变化
    5. 浏览器保持打开状态
    """

    def __init__(
        self,
        config: OddsSubscriptionConfig,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self._log = logger or logging.getLogger(self.__class__.__name__)

        # Playwright 组件
        self._playwright = None
        self._browser: Browser | None = None
        self._page: Page | None = None

        # 订阅管理
        self._subscribed_events: dict[str, dict] = {}  # event_id -> event_info
        self._latest_odds: dict[str, dict[str, dict]] = {}  # event_id -> {market_type: odds}

        # 监听任务
        self._monitor_task: asyncio.Task | None = None

        # 回调函数
        self._price_update_callback: Callable[[dict], None] | None = None

    # =========================================================================
    # 浏览器生命周期
    # =========================================================================

    async def start(self) -> None:
        """
        启动客户端

        打开浏览器并登录 OrbitExch
        """
        self._log.info("Starting OrbitExch odds client...")

        # 1. 启动 Playwright
        self._playwright = await async_playwright().start()

        # 2. 创建浏览器（保持打开）
        self._browser = await self._playwright.chromium.launch(
            headless=False,  # 可见模式，方便调试
        )

        # 3. 创建页面
        self._page = await self._browser.new_page()

        # 4. 登录
        if self.config.orbitexch_username and self.config.orbitexch_password:
            await self._login()
        else:
            self._log.warning("No OrbitExch credentials provided, skipping login")

        self._log.info("OrbitExch odds client started (browser is open)")

    async def _login(self) -> None:
        """登录 OrbitExch"""
        self._log.info("Logging in to OrbitExch...")

        try:
            # 导航到首页
            await self._page.goto(self.config.orbitexch_base_url, wait_until="networkidle")

            # 等待登录表单
            await self._page.wait_for_selector('input[name="username"]', timeout=10000)

            # 填写用户名和密码
            await self._page.fill('input[name="username"]', self.config.orbitexch_username)
            await self._page.fill('input[name="password"]', self.config.orbitexch_password)

            # 点击登录按钮
            await self._page.click('button[type="submit"]:has-text("Log In")')

            # 等待登录成功
            await self._page.wait_for_url('**/customer/**', timeout=15000)

            # 处理可能的弹窗
            await self._dismiss_popup()

            self._log.info("Login successful")

        except Exception as e:
            self._log.error(f"Login failed: {e}")
            raise

    async def _dismiss_popup(self) -> None:
        """关闭登录后的弹窗"""
        try:
            await asyncio.sleep(2)
            ok_button = self._page.locator('xpath=//button[normalize-space()="OK"]')

            if await ok_button.is_visible(timeout=5000):
                await ok_button.click()
                await asyncio.sleep(1)
                self._log.info("Popup dismissed")

        except Exception:
            pass  # 没有弹窗

    async def stop(self) -> None:
        """
        停止客户端

        关闭浏览器
        """
        self._log.info("Stopping OrbitExch odds client...")

        # 停止监听任务
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        # 关闭浏览器
        if self._browser:
            await self._browser.close()

        # 停止 Playwright
        if self._playwright:
            await self._playwright.stop()

        self._log.info("OrbitExch odds client stopped")

    # =========================================================================
    # 订阅管理
    # =========================================================================

    async def subscribe_competition(
        self,
        sport_id: str,
        competition_id: str,
        event_ids: list[str],
    ) -> None:
        """
        订阅 competition 的赔率

        浏览器导航到 competition 页面并持续监听

        Args:
            sport_id: sport ID
            competition_id: competition ID
            event_ids: 要监听的 event IDs
        """
        self._log.info(
            f"Subscribing to competition: sport_id={sport_id}, "
            f"competition_id={competition_id}, events={len(event_ids)}"
        )

        # 1. 导航到 competition 页面
        url = f"{self.config.orbitexch_base_url}/customer/sport/{sport_id}/competition/{competition_id}"
        await self._page.goto(url, wait_until="networkidle")

        # 2. 设置页面缩放
        await self._set_page_zoom(self.config.orbitexch_zoom_level)

        # 3. 等待页面加载
        await asyncio.sleep(2)

        # 4. 记录订阅的 events
        for event_id in event_ids:
            self._subscribed_events[event_id] = {
                "sport_id": sport_id,
                "competition_id": competition_id,
            }

        # 5. 启动监听任务（如果还未启动）
        if not self._monitor_task or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_odds())

        self._log.info(f"Subscribed to {len(event_ids)} events in competition {competition_id}")

    async def _set_page_zoom(self, zoom_level: float) -> None:
        """
        设置页面缩放

        Args:
            zoom_level: 缩放比例（0.8 = 80%）
        """
        self._log.info(f"Setting page zoom to {zoom_level}")

        await self._page.evaluate(f'document.body.style.zoom = "{zoom_level}"')

    # =========================================================================
    # 赔率监听
    # =========================================================================

    async def _monitor_odds(self) -> None:
        """
        持续监听页面上的赔率变化

        通过定期抓取页面数据实现
        """
        self._log.info("Starting odds monitoring loop")

        while True:
            try:
                # 抓取当前页面的所有赔率
                await self._extract_odds_from_page()

                # 每 2 秒更新一次
                await asyncio.sleep(2)

            except asyncio.CancelledError:
                self._log.info("Odds monitoring loop cancelled")
                break
            except Exception as e:
                self._log.error(f"Error in odds monitoring: {e}")
                await asyncio.sleep(5)

    async def _extract_odds_from_page(self) -> None:
        """
        从页面提取赔率数据

        使用 Playwright 的 evaluate 执行页面 JavaScript
        """
        try:
            # 执行 JavaScript 提取赔率
            odds_data = await self._page.evaluate("""
                () => {
                    const events = [];

                    // 查找所有比赛行（根据实际页面结构调整选择器）
                    const rows = document.querySelectorAll('[role="row"]');

                    rows.forEach(row => {
                        try {
                            // 提取比赛信息
                            const teamElements = row.querySelectorAll('p');
                            if (teamElements.length < 2) return;

                            const homeTeam = teamElements[0]?.textContent?.trim();
                            const awayTeam = teamElements[1]?.textContent?.trim();

                            // 提取赔率（根据实际页面结构调整）
                            // 通常是：主胜 back/lay | 平局 back/lay | 客胜 back/lay
                            const oddsButtons = row.querySelectorAll('button, [class*="odds"]');

                            const event = {
                                home_team: homeTeam,
                                away_team: awayTeam,
                                markets: {}
                            };

                            // 简化版：提取前6个赔率按钮（home back/lay, draw back/lay, away back/lay）
                            if (oddsButtons.length >= 4) {
                                event.markets.home = {
                                    back: parseFloat(oddsButtons[0]?.textContent) || 0,
                                    lay: parseFloat(oddsButtons[1]?.textContent) || 0,
                                };
                                event.markets.away = {
                                    back: parseFloat(oddsButtons[2]?.textContent) || 0,
                                    lay: parseFloat(oddsButtons[3]?.textContent) || 0,
                                };

                                // 如果有平局（6个按钮）
                                if (oddsButtons.length >= 6) {
                                    event.markets.draw = {
                                        back: parseFloat(oddsButtons[4]?.textContent) || 0,
                                        lay: parseFloat(oddsButtons[5]?.textContent) || 0,
                                    };
                                }
                            }

                            if (Object.keys(event.markets).length > 0) {
                                events.push(event);
                            }
                        } catch (e) {
                            // 忽略单个行的错误
                        }
                    });

                    return events;
                }
            """)

            # 处理提取的数据
            for event_data in odds_data:
                await self._process_extracted_odds(event_data)

        except Exception as e:
            self._log.debug(f"Failed to extract odds from page: {e}")

    async def _process_extracted_odds(self, event_data: dict) -> None:
        """
        处理提取的赔率数据

        Args:
            event_data: 从页面提取的赔率数据
        """
        # 尝试匹配到订阅的 events
        # 这里需要根据队名匹配（简化版本）
        home_team = event_data.get("home_team", "")
        away_team = event_data.get("away_team", "")
        markets = event_data.get("markets", {})

        if not markets:
            return

        # 为每个 market type 创建 odds_data
        timestamp = int(asyncio.get_event_loop().time() * 1000)

        for market_type, odds in markets.items():
            odds_data = {
                "home_team": home_team,
                "away_team": away_team,
                "market_type": market_type,
                "back": odds.get("back", 0),
                "lay": odds.get("lay", 0),
                "timestamp": timestamp,
            }

            # 生成临时 event_id（基于队名）
            event_key = f"{home_team}_vs_{away_team}"

            # 更新缓存
            if event_key not in self._latest_odds:
                self._latest_odds[event_key] = {}

            self._latest_odds[event_key][market_type] = odds_data

            # 触发回调
            if self._price_update_callback:
                self._price_update_callback(odds_data)

    # =========================================================================
    # 回调管理
    # =========================================================================

    def on_price_update(self, callback: Callable[[dict], None]) -> None:
        """
        设置价格更新回调

        Args:
            callback: 回调函数，接收 odds_data 字典
        """
        self._price_update_callback = callback

    # =========================================================================
    # 数据访问
    # =========================================================================

    def get_latest_odds(self, event_key: str) -> dict[str, dict]:
        """
        获取 event 的最新赔率

        Args:
            event_key: event key (如 "TeamA_vs_TeamB")

        Returns:
            {market_type: odds_data}
        """
        return self._latest_odds.get(event_key, {})

    # =========================================================================
    # 页面刷新
    # =========================================================================

    async def refresh_page(self) -> None:
        """
        刷新当前页面

        用于超时重连
        """
        if self._page:
            self._log.info("Refreshing page...")
            await self._page.reload(wait_until="networkidle")
            await asyncio.sleep(2)
            self._log.info("Page refreshed")
