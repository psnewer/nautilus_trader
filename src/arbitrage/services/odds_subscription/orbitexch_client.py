"""
OrbitExch 赔率客户端

使用 Playwright 拦截 OrbitExch WebSocket 获取实时赔率数据。

关键特性：
- 浏览器长期打开
- 拦截 WebSocket 消息获取实时赔率推送
- 支持页面缩放以显示更多比赛
"""

import asyncio
import json
import logging
import time
from typing import Any, Callable

from playwright.async_api import async_playwright, Page, Browser, CDPSession


from .config import OddsSubscriptionConfig


class OrbitExchOddsClient:
    """
    OrbitExch 赔率客户端

    使用 Playwright 拦截 WebSocket 消息：
    1. 打开浏览器并登录 OrbitExch
    2. 导航到指定 competition 页面
    3. 拦截 WebSocket 消息获取实时赔率
    4. 浏览器保持打开状态
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
        self._context = None
        self._pages: dict[str, Page] = {}  # competition_id -> Page (支持多个标签页)
        self._cdp_sessions: dict[str, CDPSession] = {}  # competition_id -> CDPSession

        # 订阅管理
        self._subscribed_events: dict[str, dict] = {}  # event_id -> event_info
        self._subscribed_competitions: set[str] = set()  # 已订阅的 competition_ids

        # Selection ID 映射（从市场发现获取）
        # selection_id -> {"pair_id": str, "market_type": str}
        self._selection_mapping: dict[str, dict] = {}

        # 市场映射（从 WebSocket 消息中解析，作为备用）
        self._market_info: dict[str, dict] = {}  # market_id -> {event_name, selections: {selection_id: name}}
        self._selection_to_market: dict[str, str] = {}  # selection_id -> market_id

        # 赔率缓存
        self._latest_odds: dict[str, dict] = {}  # cache_key -> odds_data

        # WebSocket 消息处理
        self._ws_messages: list[dict] = []

        # 回调函数
        self._price_update_callback: Callable[[dict], None] | None = None

        # 状态
        self._running = False

    # =========================================================================
    # 浏览器生命周期
    # =========================================================================

    async def start(self) -> None:
        """
        启动客户端

        打开浏览器并登录 OrbitExch
        """
        self._log.info("Starting OrbitExch odds client...")
        self._running = True

        # 1. 启动 Playwright
        self._playwright = await async_playwright().start()

        # 2. 创建浏览器（保持打开）
        self._browser = await self._playwright.chromium.launch(
            headless=False,  # 可见模式
        )

        # 3. 创建浏览器上下文
        self._context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
        )

        # 4. 创建主页面用于登录
        main_page = await self._context.new_page()
        self._pages["main"] = main_page

        # 5. 登录
        if self.config.orbitexch_username and self.config.orbitexch_password:
            await self._login(main_page)
        else:
            self._log.warning("No OrbitExch credentials provided, skipping login")
            await main_page.goto(self.config.orbitexch_base_url, wait_until="networkidle")

        self._log.info("OrbitExch odds client started (browser is open)")

    async def _setup_websocket_interception(self, page: Page, competition_id: str) -> None:
        """
        设置 WebSocket 消息拦截

        Args:
            page: 浏览器页面
            competition_id: competition ID（用于标识）
        """
        self._log.info(f"Setting up WebSocket interception for competition {competition_id}...")

        # 使用 CDP (Chrome DevTools Protocol) 拦截 WebSocket
        cdp_session = await self._context.new_cdp_session(page)

        # 保存 CDP 会话
        self._cdp_sessions[competition_id] = cdp_session

        # 启用网络事件
        await cdp_session.send("Network.enable")

        # 监听 WebSocket 创建事件
        cdp_session.on("Network.webSocketCreated", self._on_websocket_created)

        # 监听 WebSocket 帧接收事件
        cdp_session.on("Network.webSocketFrameReceived", self._on_websocket_frame)

        self._log.info(f"WebSocket interception enabled for competition {competition_id}")

    def _on_websocket_created(self, event: dict) -> None:
        """
        WebSocket 创建事件

        Args:
            event: CDP 事件数据
        """
        url = event.get("url", "")
        request_id = event.get("requestId", "")
        self._log.info(f"WebSocket created: {url} (requestId: {request_id})")

    def _on_websocket_frame(self, event: dict) -> None:
        """
        处理 WebSocket 帧

        Args:
            event: CDP 事件数据
        """
        try:
            payload_data = event.get("response", {}).get("payloadData", "")

            if not payload_data:
                return

            # 记录原始消息（用于调试）
            self._log.debug(f"WebSocket frame received: {payload_data[:800]}")

            # 解析 SockJS 格式消息: a["..."]
            if payload_data.startswith('a['):
                try:
                    inner_data = json.loads(payload_data[1:])  # 去掉开头的 'a'
                    if inner_data and isinstance(inner_data, list):
                        for item in inner_data:
                            try:
                                data = json.loads(item)
                                asyncio.create_task(self._process_websocket_message(data))
                            except json.JSONDecodeError:
                                pass
                except json.JSONDecodeError:
                    pass
                return

            # 尝试直接解析 JSON
            try:
                data = json.loads(payload_data)
                asyncio.create_task(self._process_websocket_message(data))
            except json.JSONDecodeError:
                pass

        except Exception as e:
            self._log.error(f"Error processing WebSocket frame: {e}")

    async def _process_websocket_message(self, data: Any) -> None:
        """
        处理 WebSocket 消息

        消息类型：
        1. 市场定义消息（包含队名、选项名称等）
        2. 价格更新消息（rc = runner changes）
        """
        if not isinstance(data, dict):
            return

        timestamp = int(time.time() * 1000)

        # 记录消息的关键字段（用于分析数据结构）
        keys = list(data.keys())
        self._log.debug(f"Message keys: {keys}")

        # 检查是否有 op（operation）字段
        op = data.get("op")
        if op:
            self._log.info(f"Operation message: op={op}, keys={keys}")

        # 处理 MCM (Market Change Message) - 包含市场定义和价格更新
        if op == "mcm" or "mc" in data:
            for market_change in data.get("mc", []):
                await self._process_market_change(market_change, timestamp)
            return

        # 处理包含 marketDefinition 的消息
        if "marketDefinition" in data:
            await self._process_market_definition(data)
            return

        # 处理 EVENT_INFO_UPDATES 消息（包含球员/队伍名称）
        if "EVENT_INFO_UPDATES" in data:
            await self._process_event_info_updates(data.get("EVENT_INFO_UPDATES", []))
            return

        # 直接的价格更新消息（只有 id 和 rc）
        market_id = data.get("id", "")
        runner_changes = data.get("rc", [])

        if market_id and runner_changes:
            await self._process_price_update(market_id, runner_changes, timestamp)
            return

    async def _process_market_definition(self, market_data: dict) -> None:
        """
        处理市场定义消息，提取队名和选项信息

        OrbitExch 数据格式：
        - id: market ID (如 "1.253315262")
        - marketDefinition: 包含 runners、marketNameWithParents 等
        - marketNameWithParents: 完整市场名 (如 "Singles Matches - Novak Djokovic v Jannik Sinner - Match Odds")
        - runners: 选项列表，只有 id 和 sortPriority，没有 name
        """
        market_id = market_data.get("id", "")

        # 记录完整的原始数据用于调试（首次看到的市场）
        if market_id and market_id not in self._market_info:
            self._log.info(f"[RAW] Market definition for {market_id}: {json.dumps(market_data)[:2000]}")

        # 获取市场定义（可能在 marketDefinition 内或直接在顶层）
        market_def = market_data.get("marketDefinition", {})

        # 提取事件名称 - 优先使用 marketNameWithParents
        event_name = ""

        # 层级1: 顶层的 marketNameWithParents（OrbitExch 特有）
        market_name_with_parents = (
            market_data.get("marketNameWithParents") or
            market_def.get("marketNameWithParents") or
            ""
        )

        if market_name_with_parents:
            # 格式: "Singles Matches - Novak Djokovic v Jannik Sinner - Match Odds"
            # 提取中间的比赛名
            event_name = self._extract_event_from_market_name(market_name_with_parents)

        # 层级2: marketDefinition 内的其他字段
        if not event_name and market_def:
            event_name = (
                market_def.get("eventName") or
                market_def.get("name") or
                market_def.get("marketName") or
                ""
            )

        # 层级3: 顶层字段
        if not event_name:
            event_name = (
                market_data.get("eventName") or
                market_data.get("name") or
                ""
            )

        # 提取 runners
        runners = market_def.get("runners", []) if market_def else []
        if not runners:
            runners = market_data.get("runners", [])

        if not market_id:
            return

        # 尝试从 eventName 解析队名 (格式: "Team A v Team B" 或 "Team A vs Team B")
        parsed_teams = self._parse_teams_from_event_name(event_name)

        # 如果无法从 eventName 解析，尝试从 EVENT_INFO_UPDATES 获取
        if not parsed_teams:
            event_id = market_def.get("eventId", "") if market_def else ""
            if event_id and hasattr(self, "_event_info"):
                event_info = self._event_info.get(str(event_id), {})
                if event_info:
                    parsed_teams = {
                        "home": event_info.get("home", ""),
                        "away": event_info.get("away", ""),
                        "draw": "The Draw",
                    }
                    # 如果 event_name 为空，构造一个
                    if not event_name and parsed_teams.get("home") and parsed_teams.get("away"):
                        event_name = f"{parsed_teams['home']} v {parsed_teams['away']}"

        # 构建选项映射
        selections = {}
        num_runners = len(runners)

        for idx, runner in enumerate(runners):
            if isinstance(runner, dict):
                # 获取 selection ID
                selection_id = str(
                    runner.get("id") or
                    runner.get("selectionId") or
                    runner.get("selection_id") or
                    ""
                )

                # 尝试多种可能的名称字段
                selection_name = (
                    runner.get("name") or
                    runner.get("runnerName") or
                    runner.get("runner_name") or
                    runner.get("n") or
                    ""
                )

                # 记录 runner 详情（仅首次）
                if market_id not in self._market_info:
                    self._log.info(f"[RUNNER] {market_id} idx={idx}: {json.dumps(runner)}")

                # 如果 runner name 是数字（sortPriority），从 eventName 推断
                sort_priority = runner.get("sortPriority", idx + 1)
                if not selection_name or selection_name in ["1", "2", "3", str(sort_priority)]:
                    # 根据 sortPriority 从解析的队名获取
                    if parsed_teams:
                        if sort_priority == 1 and "home" in parsed_teams:
                            selection_name = parsed_teams["home"]
                        elif sort_priority == 2 and num_runners == 3:
                            # 只有 3 个选项时，第二个是平局
                            selection_name = parsed_teams.get("draw", "The Draw")
                        elif (sort_priority == 2 and num_runners == 2) or sort_priority == 3:
                            # 2 选项时第二个是客队，3 选项时第三个是客队
                            selection_name = parsed_teams.get("away", f"position_{sort_priority}")
                        else:
                            selection_name = f"position_{sort_priority}"
                    else:
                        selection_name = f"position_{sort_priority}"

                if selection_id:
                    selections[selection_id] = selection_name
                    self._selection_to_market[selection_id] = market_id

        # 保存市场信息
        if selections or event_name:
            self._market_info[market_id] = {
                "event_name": event_name,
                "selections": selections,
                "parsed_teams": parsed_teams,
            }

            self._log.info(
                f"Market: id={market_id}, event='{event_name}', "
                f"teams={parsed_teams}, selections={selections}"
            )

    def _extract_event_from_market_name(self, market_name_with_parents: str) -> str:
        """
        从 marketNameWithParents 提取比赛名称

        格式: "Category - Team A v Team B - Market Type"
        例如: "Singles Matches - Novak Djokovic v Jannik Sinner - Match Odds"

        Returns:
            比赛名称 (如 "Novak Djokovic v Jannik Sinner")
        """
        if not market_name_with_parents:
            return ""

        # 按 " - " 分割
        parts = market_name_with_parents.split(" - ")

        if len(parts) >= 3:
            # 中间部分是比赛名称
            return parts[1].strip()
        elif len(parts) == 2:
            # 可能是 "Team A v Team B - Match Odds" 格式
            # 检查第一部分是否包含 " v " 或 " vs "
            if " v " in parts[0].lower() or " vs " in parts[0].lower():
                return parts[0].strip()
            return parts[0].strip()

        return market_name_with_parents

    def _parse_teams_from_event_name(self, event_name: str) -> dict[str, str]:
        """
        从 eventName 解析主客队名

        常见格式:
        - "Team A v Team B"
        - "Team A vs Team B"
        - "Team A - Team B"

        Returns:
            {"home": "Team A", "away": "Team B", "draw": "The Draw"} 或空字典
        """
        if not event_name:
            return {}

        import re

        # 尝试不同的分隔符
        separators = [" v ", " vs ", " vs. ", " @ "]

        for sep in separators:
            if sep in event_name.lower():
                # 使用大小写不敏感的分割
                parts = re.split(re.escape(sep), event_name, flags=re.IGNORECASE, maxsplit=1)
                if len(parts) == 2:
                    home = parts[0].strip()
                    away = parts[1].strip()
                    return {
                        "home": home,
                        "away": away,
                        "draw": "The Draw",
                    }

        return {}

    async def _process_event_info_updates(self, updates: list) -> None:
        """
        处理 EVENT_INFO_UPDATES 消息，提取球员/队伍名称

        数据格式:
        {
            "eventId": "35200248",
            "score": {
                "home": {"localizedNames": {"global|en": "Novak Djokovic", ...}},
                "away": {"localizedNames": {"global|en": "Jannik Sinner", ...}}
            }
        }
        """
        for update in updates:
            if not isinstance(update, dict):
                continue

            event_id = update.get("eventId", "")
            score = update.get("score", {})

            if not score:
                continue

            # 提取主客队名称
            home_info = score.get("home", {})
            away_info = score.get("away", {})

            home_names = home_info.get("localizedNames", {})
            away_names = away_info.get("localizedNames", {})

            # 优先使用英文名
            home_name = home_names.get("global|en", "")
            away_name = away_names.get("global|en", "")

            if home_name and away_name:
                # 保存到事件信息缓存
                if not hasattr(self, "_event_info"):
                    self._event_info = {}

                self._event_info[event_id] = {
                    "home": home_name,
                    "away": away_name,
                }

                self._log.info(f"Event info: {event_id} -> {home_name} v {away_name}")

    async def _process_market_change(self, market_change: dict, timestamp: int) -> None:
        """
        处理市场变化消息

        包含 marketDefinition 和/或 rc（价格更新）
        """
        market_id = market_change.get("id", "")

        # 处理市场定义
        if "marketDefinition" in market_change:
            await self._process_market_definition(market_change)

        # 处理价格更新
        runner_changes = market_change.get("rc", [])
        if runner_changes:
            await self._process_price_update(market_id, runner_changes, timestamp)

    async def _process_price_update(
        self,
        market_id: str,
        runner_changes: list,
        timestamp: int
    ) -> None:
        """
        处理价格更新

        使用 selection_mapping 匹配 selection_id 到 pair_id 和 market_type。
        只关心最佳赔率。

        runner_changes 格式：
        [{
            "id": 39674645,      // selection ID
            "bdatb": [...],      // best available to back
            "bdatl": [...]       // best available to lay
        }]
        """
        for runner in runner_changes:
            if not isinstance(runner, dict):
                continue

            selection_id = str(runner.get("id", ""))

            # 优先使用 selection_mapping 匹配
            selection_info = self._selection_mapping.get(selection_id)

            if selection_info:
                # 找到匹配的 selection
                pair_id = selection_info["pair_id"]
                market_type = selection_info["market_type"]

                # 提取最佳 back 价格
                bdatb = runner.get("bdatb", [])
                back_price = self._extract_best_price(bdatb)

                # 提取最佳 lay 价格
                bdatl = runner.get("bdatl", [])
                lay_price = self._extract_best_price(bdatl)

                # 跳过没有价格的数据
                if not back_price and not lay_price:
                    continue

                # 构造赔率数据
                odds_data = {
                    "pair_id": pair_id,
                    "selection_id": selection_id,
                    "market_type": market_type,
                    "back": back_price,
                    "lay": lay_price,
                    "timestamp": timestamp,
                }

                # 更新缓存
                cache_key = f"{pair_id}_{market_type}"
                self._latest_odds[cache_key] = odds_data

                # 触发回调
                if self._price_update_callback:
                    self._price_update_callback(odds_data)

                self._log.debug(
                    f"OrbitExch: {pair_id} {market_type} back={back_price} lay={lay_price}"
                )
            else:
                # 未在 selection_mapping 中找到，记录到 _unmatched（用于调试）
                bdatb = runner.get("bdatb", [])
                bdatl = runner.get("bdatl", [])
                back_price = self._extract_best_price(bdatb)
                lay_price = self._extract_best_price(bdatl)

                if back_price or lay_price:
                    self._log.debug(
                        f"Unmatched selection: {selection_id} back={back_price} lay={lay_price}"
                    )


    def _extract_best_price(self, price_data: list) -> float:
        """
        从 bdatb/bdatl 数据中提取最佳价格

        支持两种格式：
        - 数组格式: [[level, price, size], ...] 如 [[0, 2.06, 100.5]]
        - 字典格式: [{"price": 2.06, "size": 100}, ...]

        Returns:
            最佳价格，如果没有则返回 0
        """
        if not price_data or not isinstance(price_data, list) or len(price_data) == 0:
            return 0

        best = price_data[0]

        # 数组格式: [level, price, size]
        if isinstance(best, list) and len(best) >= 2:
            return float(best[1])  # price 在第二个位置

        # 字典格式
        if isinstance(best, dict):
            return float(best.get("price", 0) or best.get("odds", 0))

        # 直接是数字
        if isinstance(best, (int, float)):
            return float(best)

        return 0

    def _infer_market_type(self, selection_name: str, selection_id: str, market_id: str = "") -> str:
        """
        推断市场类型（主胜/平局/客胜）

        根据选项名称与 parsed_teams 匹配

        Args:
            selection_name: 选项名称
            selection_id: 选项 ID
            market_id: 市场 ID（直接传入避免查找错误，因为同一个 selection_id 可能在多个市场中）
        """
        name_lower = selection_name.lower() if selection_name else ""

        # 1. 常见的平局关键词
        if "draw" in name_lower or "the draw" in name_lower or name_lower == "x":
            return "draw"

        # 2. 使用 parsed_teams 匹配队名
        # 优先使用传入的 market_id，否则尝试查找（可能不准确）
        if not market_id:
            market_id = self._selection_to_market.get(selection_id, "")
        if market_id:
            market_info = self._market_info.get(market_id, {})
            parsed_teams = market_info.get("parsed_teams", {})

            if parsed_teams:
                home_team = parsed_teams.get("home", "").lower()
                away_team = parsed_teams.get("away", "").lower()

                # 精确匹配或包含匹配
                if home_team and (name_lower == home_team or home_team in name_lower or name_lower in home_team):
                    return "home"
                if away_team and (name_lower == away_team or away_team in name_lower or name_lower in away_team):
                    return "away"

        # 3. 如果还是无法确定，使用 selection_id 的数值排序（作为后备）
        if market_id:
            market_info = self._market_info.get(market_id, {})
            selections = market_info.get("selections", {})

            # 使用数值排序而非字符串排序
            try:
                selection_ids = sorted(selections.keys(), key=lambda x: int(x))

                if len(selection_ids) >= 2:
                    if selection_id == selection_ids[0]:
                        return "home"
                    elif len(selection_ids) == 2 and selection_id == selection_ids[1]:
                        return "away"
                    elif len(selection_ids) == 3:
                        if selection_id == selection_ids[1]:
                            return "draw"
                        elif selection_id == selection_ids[2]:
                            return "away"
            except (ValueError, TypeError):
                pass

        # 默认使用选项名称
        return selection_name or selection_id

    async def _login(self, page: Page) -> None:
        """
        登录 OrbitExch

        Args:
            page: 浏览器页面
        """
        self._log.info("Logging in to OrbitExch...")

        try:
            # 导航到首页
            await page.goto(self.config.orbitexch_base_url, wait_until="networkidle")

            # 等待登录表单
            await page.wait_for_selector('input[name="username"]', timeout=10000)

            # 填写用户名和密码
            await page.fill('input[name="username"]', self.config.orbitexch_username)
            await page.fill('input[name="password"]', self.config.orbitexch_password)

            # 点击登录按钮
            await page.click('button[type="submit"]:has-text("Log In")')

            # 等待登录成功
            await page.wait_for_url('**/customer/**', timeout=15000)

            # 处理可能的弹窗
            await self._dismiss_popup(page)

            self._log.info("Login successful")

        except Exception as e:
            self._log.error(f"Login failed: {e}")
            raise

    async def _dismiss_popup(self, page: Page) -> None:
        """
        关闭登录后的弹窗

        Args:
            page: 浏览器页面
        """
        try:
            await asyncio.sleep(2)
            ok_button = page.locator('xpath=//button[normalize-space()="OK"]')

            if await ok_button.is_visible(timeout=5000):
                await ok_button.click()
                await asyncio.sleep(1)
                self._log.info("Popup dismissed")

        except Exception:
            pass  # 没有弹窗

    async def stop(self) -> None:
        """
        停止客户端

        关闭所有标签页和浏览器
        """
        self._log.info("Stopping OrbitExch odds client...")
        self._running = False

        # 关闭所有 CDP 会话
        for comp_id, cdp_session in self._cdp_sessions.items():
            try:
                await cdp_session.detach()
            except Exception:
                pass

        self._cdp_sessions.clear()
        self._pages.clear()
        self._subscribed_competitions.clear()

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

        为每个 competition 创建一个新的浏览器标签页

        Args:
            sport_id: sport ID
            competition_id: competition ID
            event_ids: 要监听的 event IDs
        """
        # 检查是否已订阅该 competition
        page_key = f"{sport_id}_{competition_id}"
        if page_key in self._subscribed_competitions:
            self._log.info(f"Competition {competition_id} already subscribed, skipping")
            return

        self._log.info(
            f"Subscribing to competition: sport_id={sport_id}, "
            f"competition_id={competition_id}, events={len(event_ids)}"
        )

        # 1. 创建新的标签页
        page = await self._context.new_page()
        self._pages[page_key] = page

        # 2. 设置 WebSocket 拦截
        await self._setup_websocket_interception(page, page_key)

        # 3. 导航到 competition 页面
        url = f"{self.config.orbitexch_base_url}/customer/sport/{sport_id}/competition/{competition_id}"
        self._log.info(f"Navigating to: {url}")

        await page.goto(url, wait_until="networkidle")

        # 4. 设置页面缩放（显示更多比赛）
        await self._set_page_zoom(page, self.config.orbitexch_zoom_level)

        # 5. 等待页面加载和 WebSocket 连接
        await asyncio.sleep(3)

        # 5.5 从 DOM 抓取比赛名称（用于没有 marketNameWithParents 的市场）
        await self._scrape_event_names_from_dom(page)

        # 6. 记录订阅
        self._subscribed_competitions.add(page_key)
        for event_id in event_ids:
            self._subscribed_events[event_id] = {
                "sport_id": sport_id,
                "competition_id": competition_id,
            }

        self._log.info(f"Subscribed to {len(event_ids)} events in competition {competition_id}")
        self._log.info(f"Total open tabs: {len(self._pages)}")

    async def _set_page_zoom(self, page: Page, zoom_level: float) -> None:
        """
        设置页面缩放

        Args:
            page: 浏览器页面
            zoom_level: 缩放比例（0.8 = 80%）
        """
        self._log.info(f"Setting page zoom to {zoom_level}")

        await page.evaluate(f'document.body.style.zoom = "{zoom_level}"')

    async def _scrape_event_names_from_dom(self, page: Page) -> None:
        """
        从页面 DOM 抓取比赛名称

        OrbitExch 页面显示比赛列表，每个比赛有：
        - 比赛名称（如 "Man Utd v Fulham"）
        - 赔率格子对应的 market_id（可能在 data 属性中）

        这个方法在 WebSocket 数据缺少 marketNameWithParents 时作为补充
        """
        self._log.info("Scraping event names from page DOM...")

        try:
            # 执行 JavaScript 抓取页面上的比赛信息
            events = await page.evaluate("""
                () => {
                    const results = [];

                    // 查找所有比赛行/卡片
                    // OrbitExch 使用不同的 class 名，需要适配
                    const eventElements = document.querySelectorAll(
                        '[class*="event"], [class*="match"], [class*="fixture"], ' +
                        '.market-container, .event-row, .coupon-row'
                    );

                    eventElements.forEach(el => {
                        // 尝试获取比赛名称
                        const nameEl = el.querySelector(
                            '[class*="event-name"], [class*="match-name"], ' +
                            '[class*="fixture-name"], .event-title, .market-name, a'
                        );

                        if (nameEl) {
                            const text = nameEl.textContent.trim();
                            // 检查是否看起来像比赛名 (包含 v, vs, @)
                            if (text && (text.includes(' v ') || text.includes(' vs ') || text.includes(' @ '))) {
                                // 尝试获取关联的 market_id
                                const marketId = el.getAttribute('data-market-id') ||
                                                 el.getAttribute('data-id') ||
                                                 el.querySelector('[data-market-id]')?.getAttribute('data-market-id') ||
                                                 '';

                                results.push({
                                    name: text,
                                    marketId: marketId
                                });
                            }
                        }
                    });

                    // 如果上面没找到，尝试更通用的方法
                    if (results.length === 0) {
                        // 查找所有包含 " v " 的文本元素
                        const allElements = document.body.querySelectorAll('*');
                        const seen = new Set();

                        allElements.forEach(el => {
                            if (el.children.length === 0) {  // 只处理叶子节点
                                const text = el.textContent.trim();
                                if (text && text.includes(' v ') && text.length < 100 && !seen.has(text)) {
                                    seen.add(text);
                                    results.push({
                                        name: text,
                                        marketId: ''
                                    });
                                }
                            }
                        });
                    }

                    return results;
                }
            """)

            self._log.info(f"Scraped {len(events)} event names from DOM")

            # 存储抓取的比赛名称
            if not hasattr(self, "_scraped_events"):
                self._scraped_events = []

            for event in events:
                name = event.get("name", "")
                market_id = event.get("marketId", "")

                self._log.info(f"  Scraped: '{name}' (market_id: {market_id})")
                self._scraped_events.append({
                    "name": name,
                    "market_id": market_id,
                })

                # 尝试更新已有的 market_info
                if market_id and market_id in self._market_info:
                    if not self._market_info[market_id].get("event_name") or \
                       self._market_info[market_id].get("event_name") == "Match Odds":
                        self._market_info[market_id]["event_name"] = name
                        parsed = self._parse_teams_from_event_name(name)
                        if parsed:
                            self._market_info[market_id]["parsed_teams"] = parsed

                # 如果没有 market_id，尝试通过已有的 event_name 匹配
                if not market_id and name:
                    for mid, minfo in self._market_info.items():
                        if minfo.get("event_name") == "Match Odds":
                            # 暂时无法匹配，后续通过比赛时间或其他方式匹配
                            pass

        except Exception as e:
            self._log.warning(f"Failed to scrape event names from DOM: {e}")

    def _update_market_info_with_scraped_data(self) -> None:
        """
        使用抓取的数据更新 market_info

        尝试通过顺序匹配将抓取的比赛名与市场关联
        """
        if not hasattr(self, "_scraped_events"):
            return

        # 获取所有缺少比赛名的市场（event_name 是 "Match Odds"）
        markets_without_names = [
            (mid, minfo) for mid, minfo in self._market_info.items()
            if minfo.get("event_name") in ["Match Odds", "", None]
        ]

        # 获取未使用的抓取比赛名
        used_names = set(
            minfo.get("event_name")
            for minfo in self._market_info.values()
            if minfo.get("event_name") not in ["Match Odds", "", None]
        )

        available_events = [
            e for e in self._scraped_events
            if e["name"] not in used_names
        ]

        # 尝试顺序匹配（OrbitExch 页面通常按比赛时间排序）
        for i, (mid, minfo) in enumerate(markets_without_names):
            if i < len(available_events):
                event = available_events[i]
                name = event["name"]

                self._market_info[mid]["event_name"] = name
                parsed = self._parse_teams_from_event_name(name)
                if parsed:
                    self._market_info[mid]["parsed_teams"] = parsed
                    # 更新 selections 名称
                    selections = minfo.get("selections", {})
                    selection_ids = sorted(selections.keys(), key=lambda x: int(x) if x.isdigit() else 0)
                    if len(selection_ids) >= 2:
                        selections[selection_ids[0]] = parsed.get("home", selections[selection_ids[0]])
                        if len(selection_ids) == 3:
                            selections[selection_ids[1]] = parsed.get("draw", "The Draw")
                            selections[selection_ids[2]] = parsed.get("away", selections.get(selection_ids[2], ""))
                        else:
                            selections[selection_ids[1]] = parsed.get("away", selections.get(selection_ids[1], ""))

                self._log.info(f"Updated market {mid} with scraped event: {name}")

    # =========================================================================
    # Selection ID 映射管理
    # =========================================================================

    def register_selection(self, selection_id: str, pair_id: str, market_type: str) -> None:
        """
        注册 selection_id 到 pair_id 和 market_type 的映射

        Args:
            selection_id: OrbitExch selection ID (data-selection-id)
            pair_id: 匹配的 pair ID
            market_type: 市场类型 (home/draw/away)
        """
        self._selection_mapping[selection_id] = {
            "pair_id": pair_id,
            "market_type": market_type,
        }
        self._log.debug(f"Registered selection: {selection_id} -> {pair_id}/{market_type}")

    def get_selection_info(self, selection_id: str) -> dict | None:
        """
        获取 selection_id 对应的 pair_id 和 market_type

        Args:
            selection_id: OrbitExch selection ID

        Returns:
            {"pair_id": str, "market_type": str} 或 None
        """
        return self._selection_mapping.get(selection_id)

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
            event_key: event key

        Returns:
            {market_type: odds_data}
        """
        return self._latest_odds.get(event_key, {})

    def get_all_odds(self) -> dict[str, dict]:
        """获取所有赔率数据"""
        return self._latest_odds.copy()

    def get_market_info(self) -> dict[str, dict]:
        """获取市场映射信息（用于调试）"""
        return self._market_info.copy()

    # =========================================================================
    # 页面刷新
    # =========================================================================

    async def refresh_page(self) -> None:
        """
        刷新所有订阅页面

        用于超时重连
        """
        self._log.info(f"Refreshing {len(self._pages)} pages...")

        for page_key, page in self._pages.items():
            if page_key == "main":
                continue  # 跳过主登录页

            try:
                await page.reload(wait_until="networkidle")
                await asyncio.sleep(1)
                self._log.info(f"Page {page_key} refreshed")
            except Exception as e:
                self._log.error(f"Failed to refresh page {page_key}: {e}")

        self._log.info("All pages refreshed")
