"""
OrbitExch 赔率客户端

使用 Playwright + CDP WebSocket 拦截获取实时赔率数据。

关键特性：
- 直接访问 competition 页面（无需从首页导航）
- 使用 CDP (Chrome DevTools Protocol) 拦截 WebSocket 消息
- 支持 SockJS 格式的消息解析
- 使用 market_id:selection_id 复合键映射
"""

import asyncio
import json
import logging
import time
from typing import Callable

from playwright.async_api import async_playwright, Page, Browser, CDPSession


from .config import OddsSubscriptionConfig


class OrbitExchOddsClient:
    """
    OrbitExch 赔率客户端

    使用 Playwright + CDP WebSocket 拦截：
    1. 打开浏览器并登录 OrbitExch（共享会话）
    2. 直接导航到指定 competition 页面
    3. 使用 CDP 拦截 WebSocket 帧获取实时赔率
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
        self._pages: dict[str, Page] = {}  # competition_id -> Page

        # 订阅管理
        self._subscribed_events: dict[str, dict] = {}  # event_id -> event_info
        self._subscribed_competitions: set[str] = set()  # 已订阅的 competition_ids

        # Selection ID 映射（从市场发现获取）
        # 使用 "market_id:selection_id" 作为键，因为 selection_id 可能在多场比赛中复用
        # key -> {"pair_id": str, "market_type": str}
        self._selection_mapping: dict[str, dict] = {}

        # Pair 信息映射（用于在订阅时通过队名重新匹配）
        # pair_id -> {"home_team": str, "away_team": str, "selections": {"home": sel_id, "draw": sel_id, "away": sel_id}}
        self._pair_info: dict[str, dict] = {}

        # 赔率缓存
        self._latest_odds: dict[str, dict] = {}  # cache_key -> odds_data

        # 未匹配的 selection 记录（用于调试）
        # composite_key -> {"back": float, "lay": float, "count": int}
        self._unmatched_selections: dict[str, dict] = {}

        # 当前订单（CURRENT_BETS）
        # market_id -> [bet_dict, ...]
        self._current_bets: dict[str, list[dict]] | None = None
        self._current_bets_event = asyncio.Event()

        # 活跃订单（sizeRemaining > 0 的 bet）
        # market_id -> [bet_dict, ...]
        self._active_orders: dict[str, list[dict]] = {}

        # 持仓（sizeMatched > 0 的 bet）
        # market_id -> [bet_dict, ...]
        self._positions: dict[str, list[dict]] = {}

        # 账户余额
        self._balance: float = 0.0

        # 订单更新回调
        self._bets_update_callback: Callable[[dict], None] | None = None

        # 余额更新回调
        self._balance_update_callback: Callable[[float], None] | None = None

        # 回调函数
        self._price_update_callback: Callable[[dict], None] | None = None
        self._page_refresh_callback: Callable[[], None] | None = None
        self._status_callbacks: list[Callable[[str, bool], None]] = []

        # 状态
        self._running = False

        # CDP 会话管理
        self._cdp_sessions: dict[str, CDPSession] = {}  # page_key -> CDPSession

        # WebSocket 连接追踪
        self._ws_request_ids: dict[str, str] = {}  # requestId -> url

        # 最后数据更新时间（毫秒时间戳），按页面独立追踪
        self._last_data_updates: dict[str, float] = {}  # page_key -> timestamp

        # 超时监控任务
        self._staleness_monitor_task: asyncio.Task | None = None
        self._staleness_check_interval = 30  # 每 30 秒检查一次数据是否过时

        # 订阅锁（防止并行创建重复标签页）
        self._subscribe_lock = asyncio.Lock()

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

        # 完整的反检测脚本（模拟真实浏览器）
        stealth_script = """
            // ========== 1. 隐藏 webdriver 标志 ==========
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
                configurable: true
            });

            // 删除 webdriver 相关属性
            delete navigator.__proto__.webdriver;

            // ========== 2. 模拟真实的 Chrome 对象 ==========
            window.chrome = {
                runtime: {
                    onMessage: {
                        addListener: function() {},
                        removeListener: function() {}
                    },
                    sendMessage: function() {},
                    connect: function() { return { onMessage: { addListener: function() {} }, postMessage: function() {} }; }
                },
                loadTimes: function() {
                    return {
                        commitLoadTime: Date.now() / 1000 - Math.random() * 10,
                        connectionInfo: "http/1.1",
                        finishDocumentLoadTime: Date.now() / 1000 - Math.random() * 5,
                        finishLoadTime: Date.now() / 1000 - Math.random() * 2,
                        firstPaintAfterLoadTime: 0,
                        firstPaintTime: Date.now() / 1000 - Math.random() * 8,
                        navigationType: "Other",
                        npnNegotiatedProtocol: "unknown",
                        requestTime: Date.now() / 1000 - Math.random() * 15,
                        startLoadTime: Date.now() / 1000 - Math.random() * 12,
                        wasAlternateProtocolAvailable: false,
                        wasFetchedViaSpdy: false,
                        wasNpnNegotiated: false
                    };
                },
                csi: function() {
                    return {
                        onloadT: Date.now(),
                        pageT: Math.random() * 10000,
                        startE: Date.now() - Math.random() * 10000,
                        tran: 15
                    };
                },
                app: {
                    isInstalled: false,
                    InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                    RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }
                }
            };

            // ========== 3. 模拟真实的 plugins ==========
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    const plugins = [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
                    ];
                    plugins.item = (i) => plugins[i];
                    plugins.namedItem = (name) => plugins.find(p => p.name === name);
                    plugins.refresh = () => {};
                    return plugins;
                },
                configurable: true
            });

            // ========== 4. 模拟真实的 mimeTypes ==========
            Object.defineProperty(navigator, 'mimeTypes', {
                get: () => {
                    const mimeTypes = [
                        { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
                        { type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format' }
                    ];
                    mimeTypes.item = (i) => mimeTypes[i];
                    mimeTypes.namedItem = (name) => mimeTypes.find(m => m.type === name);
                    return mimeTypes;
                },
                configurable: true
            });

            // ========== 5. 模拟真实的 languages ==========
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
                configurable: true
            });

            // ========== 6. 修复 permissions API ==========
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );

            // ========== 7. 隐藏自动化痕迹 ==========
            // 修复 navigator.connection
            if (navigator.connection === undefined) {
                Object.defineProperty(navigator, 'connection', {
                    get: () => ({
                        effectiveType: '4g',
                        rtt: 50,
                        downlink: 10,
                        saveData: false
                    }),
                    configurable: true
                });
            }

            // ========== 8. 模拟真实的硬件并发数 ==========
            Object.defineProperty(navigator, 'hardwareConcurrency', {
                get: () => 8,
                configurable: true
            });

            // ========== 9. 模拟真实的设备内存 ==========
            Object.defineProperty(navigator, 'deviceMemory', {
                get: () => 8,
                configurable: true
            });

            // ========== 10. 隐藏 Playwright/Puppeteer 特征 ==========
            // 删除 window 上的自动化相关属性
            const automationProps = [
                '__playwright',
                '__puppeteer_evaluation_script__',
                '__driver_evaluate',
                '__webdriver_evaluate',
                '__selenium_evaluate',
                '__fxdriver_evaluate',
                '__driver_unwrapped',
                '__webdriver_unwrapped',
                '__selenium_unwrapped',
                '__fxdriver_unwrapped',
                '_Selenium_IDE_Recorder',
                '_selenium',
                'calledSelenium',
                '$cdc_asdjflasutopfhvcZLmcfl_',
                '$chrome_asyncScriptInfo',
                '__$webdriverAsyncExecutor',
                'webdriver',
                '__webdriver_script_fn'
            ];

            for (const prop of automationProps) {
                if (prop in window) {
                    delete window[prop];
                }
            }

            // ========== 11. 修复 iframe contentWindow ==========
            const originalContentWindow = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
            Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
                get: function() {
                    const result = originalContentWindow.get.call(this);
                    if (result) {
                        // 确保 iframe 中的 navigator.webdriver 也被隐藏
                        try {
                            Object.defineProperty(result.navigator, 'webdriver', {
                                get: () => undefined,
                                configurable: true
                            });
                        } catch (e) {}
                    }
                    return result;
                }
            });

            // ========== 12. 模拟 WebGL 指纹 ==========
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) {
                    return 'Intel Inc.';
                }
                if (parameter === 37446) {
                    return 'Intel Iris OpenGL Engine';
                }
                return getParameter.call(this, parameter);
            };

            console.log('[Stealth] Anti-detection script loaded');
        """

        # 浏览器启动参数（反检测）
        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-infobars",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=IsolateOrigins,site-per-process,TranslateUI",
            "--disable-ipc-flooding-protection",
            "--enable-features=NetworkService,NetworkServiceInProcess",
            "--force-color-profile=srgb",
            "--metrics-recording-only",
            "--no-first-run",
            "--password-store=basic",
            "--use-mock-keychain",
            "--export-tagged-pdf",
            "--disable-popup-blocking",
        ]

        # 2. 使用持久化上下文（如果配置了 user_data_dir）
        if self.config.orbitexch_user_data_dir:
            self._log.info(f"Using persistent context: {self.config.orbitexch_user_data_dir}")
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=self.config.orbitexch_user_data_dir,
                headless=False,
                channel="chrome",  # 使用系统 Chrome，避免 bundled Chromium 崩溃
                args=browser_args,
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                locale="en-US",
                timezone_id="America/New_York",
                color_scheme="light",
                ignore_https_errors=True,
            )
            self._browser = self._context.browser

        # 3. 启动新浏览器（默认）
        else:
            self._browser = await self._playwright.chromium.launch(
                headless=False,
                channel="chrome",  # 使用系统 Chrome，避免 bundled Chromium 崩溃
                args=browser_args,
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                locale="en-US",
                timezone_id="America/New_York",
                color_scheme="light",
                ignore_https_errors=True,
            )

        # 添加反检测脚本
        await self._context.add_init_script(stealth_script)

        # 添加可见性欺骗脚本（让 WebSocket 推送所有比赛的赔率）
        visibility_spoof_script = """
            // ========== 拦截 IntersectionObserver ==========
            // 网站可能使用 IntersectionObserver 来检测元素是否在视口内
            // 我们欺骗它，让所有元素都被报告为可见

            const OriginalIntersectionObserver = window.IntersectionObserver;

            window.IntersectionObserver = function(callback, options) {
                // 创建一个修改过的回调函数，始终报告元素可见
                const spoofedCallback = (entries, observer) => {
                    const spoofedEntries = entries.map(entry => {
                        // 创建一个代理对象，报告元素完全可见
                        return new Proxy(entry, {
                            get(target, prop) {
                                if (prop === 'isIntersecting') return true;
                                if (prop === 'intersectionRatio') return 1.0;
                                if (prop === 'isVisible') return true;
                                return target[prop];
                            }
                        });
                    });
                    callback(spoofedEntries, observer);
                };

                const observer = new OriginalIntersectionObserver(spoofedCallback, options);

                // 保存原始的 observe 方法
                const originalObserve = observer.observe.bind(observer);

                // 重写 observe 方法，立即触发一次可见回调
                observer.observe = function(target) {
                    originalObserve(target);

                    // 立即触发一次"可见"回调
                    setTimeout(() => {
                        try {
                            const rect = target.getBoundingClientRect();
                            // 创建一个模拟的 entry
                            const fakeEntry = {
                                boundingClientRect: rect,
                                intersectionRatio: 1.0,
                                intersectionRect: rect,
                                isIntersecting: true,
                                isVisible: true,
                                rootBounds: null,
                                target: target,
                                time: performance.now()
                            };
                            callback([fakeEntry], observer);
                        } catch(e) {}
                    }, 10);
                };

                return observer;
            };

            // 复制原型
            window.IntersectionObserver.prototype = OriginalIntersectionObserver.prototype;

            console.log('[Visibility Spoof] IntersectionObserver intercepted - all elements will appear visible');
        """
        await self._context.add_init_script(visibility_spoof_script)

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
            # 等待弹窗出现
            await asyncio.sleep(3)

            # 直接使用已知有效的选择器
            selector = 'xpath=//button[normalize-space()="OK"]'
            try:
                button = page.locator(selector)
                # 等待按钮可见，最多等待 5 秒
                await button.wait_for(state="visible", timeout=5000)
                await button.click()
                self._log.info("Popup dismissed (OK button)")
                await asyncio.sleep(0.5)
                return
            except Exception as e:
                self._log.debug(f"OK button not found or not clickable: {e}")

            # 备用选择器
            backup_selectors = [
                'button:has-text("OK")',
                'button:has-text("Close")',
                '[aria-label="Close"]',
            ]

            for sel in backup_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        self._log.info(f"Popup dismissed using: {sel}")
                        await asyncio.sleep(0.5)
                        return
                except Exception:
                    continue

            self._log.debug("No popup found to dismiss")

        except Exception as e:
            self._log.debug(f"Error dismissing popup: {e}")

    async def stop(self) -> None:
        """
        停止客户端

        关闭所有标签页和浏览器
        """
        self._log.info("Stopping OrbitExch odds client...")
        self._running = False

        # 停止超时监控任务
        if self._staleness_monitor_task and not self._staleness_monitor_task.done():
            self._staleness_monitor_task.cancel()
            try:
                await self._staleness_monitor_task
            except asyncio.CancelledError:
                pass

        # 断开 CDP 会话
        for page_key, cdp_session in list(self._cdp_sessions.items()):
            try:
                await cdp_session.detach()
                self._log.debug(f"Detached CDP session for {page_key}")
            except Exception:
                pass
        self._cdp_sessions.clear()

        self._pages.clear()
        self._subscribed_competitions.clear()
        self._ws_request_ids.clear()

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

        使用 CDP WebSocket 拦截获取实时赔率数据。
        关键：必须在登录后（共享会话）才能接收到 WebSocket 数据。

        Args:
            sport_id: sport ID
            competition_id: competition ID
            event_ids: 要监听的 event IDs
        """
        page_key = f"{sport_id}_{competition_id}"

        # 使用锁防止并行创建重复标签页
        async with self._subscribe_lock:
            # 检查是否已订阅该 competition
            if page_key in self._subscribed_competitions:
                self._log.info(f"Competition {competition_id} already subscribed, skipping")
                return

            # 立即标记为已订阅，防止其他任务重复创建
            self._subscribed_competitions.add(page_key)

        self._log.info(
            f"Subscribing to competition: sport_id={sport_id}, "
            f"competition_id={competition_id}, events={len(event_ids)}"
        )

        url = f"{self.config.orbitexch_base_url}/customer/sport/{sport_id}/competition/{competition_id}"

        try:
            # 1. 创建新标签页
            page = await self._context.new_page()
            self._pages[page_key] = page
            self._log.info(f"Created new tab for competition {competition_id}")

            # 2. 【关键】在导航之前设置 CDP WebSocket 拦截
            await self._setup_websocket_interception(page, page_key)

            # 3. 导航到 competition 页面
            self._log.info(f"Navigating to: {url}")
            await page.goto(url, wait_until="networkidle", timeout=60000)

            # 4. 等待页面加载和 WebSocket 连接
            await asyncio.sleep(3)

            # 5. 注入可见性欺骗脚本（让所有比赛都接收 WebSocket 数据）
            await self._setup_visibility_spoof(page)

            # 6. 抓取页面上的比赛信息并更新 selection 映射
            await self._refresh_selection_mapping_from_page(page)

            # 7. 启动超时监控任务
            if self._staleness_monitor_task is None or self._staleness_monitor_task.done():
                self._staleness_monitor_task = asyncio.create_task(self._staleness_monitor_loop())
                self._log.info("Started staleness monitor task")

            # 8. 记录订阅的 events
            for event_id in event_ids:
                self._subscribed_events[event_id] = {
                    "sport_id": sport_id,
                    "competition_id": competition_id,
                }

            self._log.info(f"Subscribed to {len(event_ids)} events in competition {competition_id}")
            self._log.info(f"Total open tabs: {len(self._pages)}")

        except Exception as e:
            # 订阅失败，清理状态以允许重试
            self._log.error(f"Failed to subscribe competition {competition_id}: {e}")
            self._subscribed_competitions.discard(page_key)
            if page_key in self._pages:
                try:
                    await self._pages[page_key].close()
                except Exception:
                    pass
                del self._pages[page_key]
            raise

    # =========================================================================
    # CDP WebSocket 拦截
    # =========================================================================

    async def _setup_websocket_interception(self, page: Page, page_key: str) -> None:
        """
        设置 CDP WebSocket 拦截

        使用 Chrome DevTools Protocol 拦截浏览器级别的 WebSocket 消息，
        这比 JavaScript 拦截更可靠。

        Args:
            page: 浏览器页面
            page_key: 页面键（用于标识）
        """
        self._log.info(f"Setting up CDP WebSocket interception for {page_key}...")

        try:
            # 创建 CDP 会话
            cdp_session = await self._context.new_cdp_session(page)
            self._cdp_sessions[page_key] = cdp_session

            # 启用网络监控
            await cdp_session.send("Network.enable")

            # 注册 WebSocket 事件处理（用闭包捕获 page_key）
            cdp_session.on("Network.webSocketCreated", self._on_websocket_created)
            cdp_session.on(
                "Network.webSocketFrameReceived",
                lambda event, pk=page_key: self._on_websocket_frame(event, pk),
            )
            cdp_session.on("Network.webSocketClosed", self._on_websocket_closed)

            self._log.info(f"CDP WebSocket interception ready for {page_key}")

        except Exception as e:
            self._log.error(f"Failed to setup CDP interception for {page_key}: {e}")
            raise

    def _on_websocket_created(self, event: dict) -> None:
        """
        WebSocket 连接创建回调

        Args:
            event: CDP 事件数据 {"requestId": str, "url": str}
        """
        request_id = event.get("requestId", "")
        url = event.get("url", "")
        self._log.info(f"WebSocket created: {url} (requestId={request_id})")

        # 追踪 WebSocket 连接
        self._ws_request_ids[request_id] = url

    def _on_websocket_closed(self, event: dict) -> None:
        """
        WebSocket 连接关闭回调

        Args:
            event: CDP 事件数据 {"requestId": str}
        """
        request_id = event.get("requestId", "")
        url = self._ws_request_ids.pop(request_id, "unknown")
        self._log.info(f"WebSocket closed: {url} (requestId={request_id})")

    def _on_websocket_frame(self, event: dict, page_key: str = "") -> None:
        """
        WebSocket 帧接收回调

        OrbitExch 使用 SockJS 协议，消息格式为 a["..."]

        Args:
            event: CDP 事件数据 {"requestId": str, "timestamp": float, "response": {"payloadData": str}}
            page_key: 来源页面标识
        """
        try:
            payload_data = event.get("response", {}).get("payloadData", "")

            # 跳过空消息
            if not payload_data:
                return

            # 解析 SockJS 格式: a["..."]
            if payload_data.startswith('a['):
                try:
                    # 去掉开头的 'a' 得到 JSON 数组
                    inner_data = json.loads(payload_data[1:])

                    for item in inner_data:
                        # 每个 item 是一个 JSON 字符串
                        try:
                            data = json.loads(item)
                            # 异步处理消息（传递 page_key 用于追踪数据新鲜度）
                            asyncio.create_task(self._process_websocket_message(data, page_key))
                        except json.JSONDecodeError:
                            pass

                except json.JSONDecodeError as e:
                    self._log.debug(f"Failed to parse SockJS message: {e}")

            # 心跳消息 'h'
            elif payload_data == 'h':
                pass  # 忽略心跳

            # 打开消息 'o'
            elif payload_data == 'o':
                self._log.debug("WebSocket SockJS opened")

        except Exception as e:
            self._log.error(f"Error processing WebSocket frame: {e}")

    async def _process_websocket_message(self, data: dict, page_key: str = "") -> None:
        """
        处理 WebSocket 消息

        OrbitExch WebSocket 消息格式（非标准 MCM）:
        - 市场数据消息: {"id": "market_id", "rc": [...], "marketDefinition": {...}, ...}
        - 事件信息消息: {"EVENT_INFO_UPDATES": [...]}
        - 当前订单消息: {"CURRENT_BETS": [...]}
        - 其他消息: {"PROPERTIES": {...}}, {"OPEN_BETS_COUNTER": ...}

        Args:
            data: 解析后的 JSON 数据
            page_key: 来源页面标识
        """
        try:
            # 检查是否是市场数据消息（包含 id 和 rc 字段）
            if "id" in data and "rc" in data:
                # 只有市场数据才更新 last_data_updates（用于 staleness 检测）
                if page_key:
                    self._last_data_updates[page_key] = time.time() * 1000
                await self._process_market_data(data)
            # 处理 CURRENT_BETS 消息（用户订单数据）
            elif "CURRENT_BETS" in data:
                await self._process_current_bets(data)
            # 处理 BALANCE 消息（账户余额）
            elif "BALANCE" in data:
                self._log.info(f"BALANCE message received: {data}")
                await self._process_balance(data)
            # 跳过其他消息类型（EVENT_INFO_UPDATES, PROPERTIES, OPEN_BETS_COUNTER）

        except Exception as e:
            self._log.error(f"Error processing WebSocket message: {e}")
            import traceback
            traceback.print_exc()

    async def _process_market_data(self, data: dict) -> None:
        """
        处理 OrbitExch 市场数据消息

        消息格式：
        {
            "id": "1.253525959",  # market_id
            "marketDefinition": {
                "inplay": true,  # 比赛是否已开始
                "runners": [
                    {"id": 6390244, "name": "Belinda Bencic", "sortPriority": 1},
                    {"id": 26309908, "name": "Sonay Kartal", "sortPriority": 2}
                ],
                ...
            },
            "rc": [  # runner changes
                {
                    "id": 6390244,
                    "batb": [[0, 1.35, 100], ...],  # best available to back
                    "batl": [[0, 1.36, 50], ...],   # best available to lay
                },
                ...
            ],
            "apiPt": 1770042012345,  # timestamp
            ...
        }

        Args:
            data: 市场数据
        """
        market_id = str(data.get("id", ""))
        timestamp = int(time.time() * 1000)

        # 从 marketDefinition 获取 inplay 状态
        market_def = data.get("marketDefinition", {})
        inplay = market_def.get("inplay", None)

        # 如果有 inplay 信息，更新对应 pair 的状态
        if inplay is not None:
            self._update_inplay_status(market_id, inplay)

        # 处理 runner changes
        rc_list = data.get("rc", [])

        for rc in rc_list:
            selection_id = str(rc.get("id", ""))

            # 提取 best back price and size
            # OrbitExch 使用 bdatb (best display available to back)
            # 格式: [{"index": 0, "odds": 3.05, "amount": 53.1}, ...]
            back_price = 0.0
            back_size = 0.0
            bdatb = rc.get("bdatb") or rc.get("batb", [])
            if bdatb and len(bdatb) > 0:
                first_back = bdatb[0]
                if isinstance(first_back, dict):
                    back_price = float(first_back.get("odds", 0))
                    back_size = float(first_back.get("amount", 0))
                elif isinstance(first_back, list) and len(first_back) > 1:
                    back_price = float(first_back[1])
                    back_size = float(first_back[2]) if len(first_back) > 2 else 0.0

            # 提取 best lay price and size
            # OrbitExch 使用 bdatl (best display available to lay)
            # 格式: [{"index": 0, "odds": 3.1, "amount": 5.55}, ...]
            lay_price = 0.0
            lay_size = 0.0
            bdatl = rc.get("bdatl") or rc.get("batl", [])
            if bdatl and len(bdatl) > 0:
                first_lay = bdatl[0]
                if isinstance(first_lay, dict):
                    lay_price = float(first_lay.get("odds", 0))
                    lay_size = float(first_lay.get("amount", 0))
                elif isinstance(first_lay, list) and len(first_lay) > 1:
                    lay_price = float(first_lay[1])
                    lay_size = float(first_lay[2]) if len(first_lay) > 2 else 0.0

            # 跳过无价格数据
            if not back_price and not lay_price:
                continue
            await self._process_odds_update({
                "marketId": market_id,
                "selectionId": selection_id,
                "back": back_price,
                "lay": lay_price,
                "back_size": back_size,
                "lay_size": lay_size,
                "timestamp": timestamp,
            })

    async def _process_current_bets(self, data: dict) -> None:
        """
        处理 CURRENT_BETS 消息（用户订单数据）

        消息格式：
        {
            "CURRENT_BETS": [
                {
                    "offerId": 212257822,
                    "marketId": "1.253930426",
                    "selectionId": 8827537,
                    "selectionName": "Team A",
                    "side": "BACK",  # "BACK" or "LAY"
                    "price": 10.00,
                    "sizePlaced": "7.00",
                    "sizeMatched": "0.00",
                    "sizeRemaining": "7.00",
                    "marketProfit": 63.00,  # 如果 selection 赢时的盈利
                    "marketLiability": 7.00,  # 如果 selection 输时的亏损
                    "eventName": "Team A v Team B",
                    "competitionName": "Premier League",
                    ...
                }
            ]
        }

        Args:
            data: 解析后的 JSON 数据
        """
        try:
            bets = data.get("CURRENT_BETS", [])

            self._log.debug(f"Received CURRENT_BETS update: {len(bets)} bets")

            # 按 market_id 分组
            bets_by_market: dict[str, list[dict]] = {}
            active_orders: dict[str, list[dict]] = {}
            positions: dict[str, list[dict]] = {}

            for bet in bets:
                market_id = str(bet.get("marketId", ""))
                if not market_id:
                    continue

                bets_by_market.setdefault(market_id, []).append(bet)

                size_remaining = float(bet.get("sizeRemaining", 0))
                size_matched = float(bet.get("sizeMatched", 0))

                # 活跃订单：还有未成交部分
                if size_remaining > 0:
                    active_orders.setdefault(market_id, []).append(bet)

                # 持仓：有已成交部分（未结算的）
                if size_matched > 0:
                    positions.setdefault(market_id, []).append(bet)

            # 更新缓存
            self._current_bets = bets_by_market
            self._active_orders = active_orders
            self._positions = positions
            self._current_bets_event.set()

            self._log.debug(
                f"CURRENT_BETS derived: {sum(len(v) for v in active_orders.values())} active orders, "
                f"{sum(len(v) for v in positions.values())} positions"
            )

            # 触发回调
            if self._bets_update_callback:
                try:
                    self._bets_update_callback({
                        "bets": bets,
                        "bets_by_market": bets_by_market,
                        "timestamp": int(time.time() * 1000),
                    })
                except Exception as e:
                    self._log.error(f"Bets update callback error: {e}")

        except Exception as e:
            self._log.error(f"Error processing CURRENT_BETS: {e}")
            import traceback
            traceback.print_exc()

    async def _process_balance(self, data: dict) -> None:
        """
        处理 BALANCE 消息（账户余额）

        消息格式：
        {"BALANCE": {"balance": "30.68", "avBalance": null}}
        """
        try:
            balance_data = data.get("BALANCE", {})
            balance_str = balance_data.get("balance")

            if balance_str is not None:
                balance = float(balance_str)
                old_balance = self._balance
                self._balance = balance

                # 升级为 INFO 级别，确保能看到
                if abs(balance - old_balance) > 0.01:
                    self._log.info(
                        f"OrbitExch balance updated: {old_balance:.2f} -> {balance:.2f}"
                    )
                else:
                    self._log.debug(
                        f"Balance unchanged: {balance:.2f}"
                    )

                # 触发回调
                if self._balance_update_callback:
                    try:
                        self._balance_update_callback(balance)
                    except Exception as e:
                        self._log.error(f"Balance callback error: {e}")
                else:
                    self._log.warning("BALANCE message received but no callback registered")
        except Exception as e:
            self._log.error(f"Error processing BALANCE: {e}")

    def get_current_bets(self, market_id: str | None = None) -> list[dict]:
        """
        获取当前订单

        Args:
            market_id: 可选，指定 market_id 返回该市场的订单

        Returns:
            订单列表
        """
        if not self._current_bets:
            return []
        if market_id:
            return self._current_bets.get(market_id, [])
        else:
            # 返回所有订单
            all_bets = []
            for bets in self._current_bets.values():
                all_bets.extend(bets)
            return all_bets

    def get_bets_by_pair(self, pair_id: str) -> list[dict]:
        """
        获取指定比赛的订单

        Args:
            pair_id: 比赛 ID

        Returns:
            订单列表
        """
        result = []

        # 从 pair_info 获取 market_id
        pair_info = self._pair_info.get(pair_id, {})
        market_id = pair_info.get("market_id")

        if market_id and self._current_bets:
            result = self._current_bets.get(market_id, [])

        return result

    def get_active_orders(self, market_id: str | None = None) -> list[dict]:
        """
        获取活跃订单（sizeRemaining > 0）

        Args:
            market_id: 可选，按 market_id 过滤

        Returns:
            活跃订单列表
        """
        if market_id:
            return self._active_orders.get(market_id, [])
        all_orders = []
        for orders in self._active_orders.values():
            all_orders.extend(orders)
        return all_orders

    def get_positions(self, market_id: str | None = None) -> list[dict]:
        """
        获取持仓（sizeMatched > 0，未结算）

        Args:
            market_id: 可选，按 market_id 过滤

        Returns:
            持仓列表
        """
        if market_id:
            return self._positions.get(market_id, [])
        all_positions = []
        for positions in self._positions.values():
            all_positions.extend(positions)
        return all_positions

    async def wait_for_current_bets(self, timeout: float) -> bool:
        """等待首次 CURRENT_BETS 到达"""
        if self._current_bets is not None:
            return True
        try:
            await asyncio.wait_for(self._current_bets_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def register_bets_callback(self, callback: Callable[[dict], None]) -> None:
        """注册订单更新回调"""
        self._bets_update_callback = callback

    def register_balance_callback(self, callback: Callable[[float], None]) -> None:
        """注册余额更新回调"""
        self._balance_update_callback = callback

    def get_balance(self) -> float:
        """获取当前余额"""
        return self._balance

    async def _process_mcm_message(self, data: dict) -> None:
        """
        处理 MCM (Market Change Message) 消息（备用，MCM 格式）

        MCM 消息格式：
        {
            "op": "mcm",
            "clk": "...",
            "pt": timestamp,
            "mc": [
                {
                    "id": "market_id",
                    "marketDefinition": {...},  # 可选，包含 selection 定义
                    "rc": [  # runner changes
                        {
                            "id": selection_id,
                            "batb": [[price_idx, price, size], ...],  # best available to back
                            "batl": [[price_idx, price, size], ...],  # best available to lay
                        }
                    ]
                }
            ]
        }

        Args:
            data: MCM 消息数据
        """
        mc_list = data.get("mc", [])
        timestamp = data.get("pt", int(time.time() * 1000))

        # 调试：记录 MCM 消息
        if mc_list:
            self._log.debug(f"MCM message: {len(mc_list)} market changes")

        for mc in mc_list:
            market_id = mc.get("id", "")

            # 处理 marketDefinition（包含 selection 信息）
            market_def = mc.get("marketDefinition")
            if market_def:
                await self._process_market_definition(market_id, market_def)

            # 处理 runner changes（赔率变化）
            rc_list = mc.get("rc", [])
            for rc in rc_list:
                selection_id = str(rc.get("id", ""))

                # 提取 best back price and size (batb)
                back_price = 0.0
                back_size = 0.0
                batb = rc.get("batb", [])
                if batb and len(batb) > 0:
                    # batb 格式: [[price_idx, price, size], ...]
                    # 取第一个（最佳价格）
                    back_price = float(batb[0][1]) if len(batb[0]) > 1 else 0.0
                    back_size = float(batb[0][2]) if len(batb[0]) > 2 else 0.0

                # 提取 best lay price and size (batl)
                lay_price = 0.0
                lay_size = 0.0
                batl = rc.get("batl", [])
                if batl and len(batl) > 0:
                    lay_price = float(batl[0][1]) if len(batl[0]) > 1 else 0.0
                    lay_size = float(batl[0][2]) if len(batl[0]) > 2 else 0.0

                # 跳过无价格数据
                if not back_price and not lay_price:
                    continue

                # 处理赔率更新
                await self._process_odds_update({
                    "marketId": market_id,
                    "selectionId": selection_id,
                    "back": back_price,
                    "lay": lay_price,
                    "back_size": back_size,
                    "lay_size": lay_size,
                    "timestamp": timestamp,
                })

    async def _process_market_definition(self, market_id: str, market_def: dict) -> None:
        """
        处理市场定义消息

        marketDefinition 包含 runners（selections）信息，可用于建立映射

        Args:
            market_id: 市场 ID
            market_def: 市场定义数据
        """
        runners = market_def.get("runners", [])

        for runner in runners:
            selection_id = str(runner.get("id", ""))
            name = runner.get("name", "")
            sort_priority = runner.get("sortPriority", 0)

            # 日志记录（用于调试映射关系）
            self._log.debug(
                f"Market definition: market={market_id}, "
                f"selection={selection_id}, name={name}, sort={sort_priority}"
            )

    async def _setup_visibility_spoof(self, page: Page) -> None:
        """
        注入可见性欺骗脚本

        欺骗网站的可见性检测，让它认为所有比赛元素都在视口内，
        从而让 WebSocket 推送所有比赛的赔率数据。
        """
        await page.evaluate("""
            () => {
                if (window.__visibilitySpoofInstalled) return;
                window.__visibilitySpoofInstalled = true;

                // ========== 1. 拦截 IntersectionObserver ==========
                // 网站可能使用 IntersectionObserver 来检测元素是否在视口内
                const OriginalIntersectionObserver = window.IntersectionObserver;

                window.IntersectionObserver = function(callback, options) {
                    // 创建一个修改过的回调函数，始终报告元素可见
                    const spoofedCallback = (entries, observer) => {
                        const spoofedEntries = entries.map(entry => {
                            // 创建一个假的 entry，报告元素完全可见
                            return {
                                boundingClientRect: entry.boundingClientRect,
                                intersectionRatio: 1.0,  // 100% 可见
                                intersectionRect: entry.boundingClientRect,
                                isIntersecting: true,    // 始终可见
                                isVisible: true,
                                rootBounds: entry.rootBounds,
                                target: entry.target,
                                time: entry.time
                            };
                        });
                        callback(spoofedEntries, observer);
                    };

                    const observer = new OriginalIntersectionObserver(spoofedCallback, options);

                    // 保存原始的 observe 方法
                    const originalObserve = observer.observe.bind(observer);

                    // 重写 observe 方法，立即触发一次可见回调
                    observer.observe = function(target) {
                        originalObserve(target);

                        // 立即触发一次"可见"回调
                        setTimeout(() => {
                            const rect = target.getBoundingClientRect();
                            const fakeEntry = {
                                boundingClientRect: rect,
                                intersectionRatio: 1.0,
                                intersectionRect: rect,
                                isIntersecting: true,
                                isVisible: true,
                                rootBounds: null,
                                target: target,
                                time: performance.now()
                            };
                            callback([fakeEntry], observer);
                        }, 10);
                    };

                    return observer;
                };

                // 复制原型和静态属性
                window.IntersectionObserver.prototype = OriginalIntersectionObserver.prototype;

                console.log('[Visibility Spoof] IntersectionObserver intercepted');

                // ========== 2. 欺骗 getBoundingClientRect ==========
                // 某些网站直接使用 getBoundingClientRect 检查元素位置
                const originalGetBoundingClientRect = Element.prototype.getBoundingClientRect;

                // 不修改 getBoundingClientRect，因为这可能破坏页面布局
                // 但我们可以监控它的调用
                Element.prototype.getBoundingClientRect = function() {
                    const rect = originalGetBoundingClientRect.call(this);

                    // 如果是赔率相关元素，记录调用（用于调试）
                    if (this.hasAttribute && this.hasAttribute('data-selection-id')) {
                        // console.log('[Visibility Spoof] getBoundingClientRect called on selection:', this.getAttribute('data-selection-id'));
                    }

                    return rect;
                };

                // ========== 3. 欺骗 scroll 事件相关的可见性检查 ==========
                // 设置一个全局标志，表示所有元素都应该被视为可见
                window.__allElementsVisible = true;

                // ========== 4. 监控可能的可见性检查函数 ==========
                // 有些框架使用自定义函数检查可见性
                const checkVisibilityFunctions = [
                    'isElementInViewport',
                    'isInViewport',
                    'isVisible',
                    'checkVisibility',
                    'isElementVisible'
                ];

                checkVisibilityFunctions.forEach(funcName => {
                    if (typeof window[funcName] === 'function') {
                        const original = window[funcName];
                        window[funcName] = function(...args) {
                            console.log(`[Visibility Spoof] ${funcName} called, returning true`);
                            return true;  // 始终返回可见
                        };
                    }
                });

                console.log('[Visibility Spoof] Visibility spoof installed');
            }
        """)
        self._log.info("Visibility spoof installed")


    async def _refresh_selection_mapping_from_page(self, page: Page) -> None:
        """
        从当前页面重新抓取比赛信息，并通过队名匹配更新 selection_mapping

        这是必要的，因为 market_id 可能在 discovery 和 subscription 之间发生变化
        （页面上的比赛排序、新增/删除等）

        同时检测比赛状态：
        - data-time-section="inPlay" -> is_live=True
        - data-time-section="comingUp" -> is_live=False
        """
        self._log.info("Refreshing selection mapping from live page...")

        try:
            # 抓取页面上所有比赛的信息（包括比赛状态）
            matches_data = await page.evaluate("""
                () => {
                    const results = [];

                    // 查找 role="row" 的元素（每行是一场比赛）
                    const rows = document.querySelectorAll('[role="row"]');

                    rows.forEach(row => {
                        // 查找队名（两个 p 元素）
                        const pElements = row.querySelectorAll('p');

                        if (pElements.length >= 2) {
                            const homeTeam = pElements[0].textContent?.trim() || '';
                            const awayTeam = pElements[1].textContent?.trim() || '';

                            if (homeTeam && awayTeam) {
                                // 提取 market_id 和 selection_ids
                                const selectionElements = row.querySelectorAll('[data-selection-id]');
                                let marketId = '';
                                let homeSelectionId = '';
                                let drawSelectionId = '';
                                let awaySelectionId = '';

                                // 获取 market_id
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
                                    homeSelectionId = selectionElements[0].getAttribute('data-selection-id') || '';
                                    awaySelectionId = selectionElements[1].getAttribute('data-selection-id') || '';
                                }

                                // 注意：比赛状态 (is_live) 将从 WebSocket 的 inplay 字段获取
                                // 这里默认为 false，等待 WebSocket 数据更新
                                if (marketId) {
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
                        }
                    });

                    return results;
                }
            """)

            self._log.info(f"Found {len(matches_data)} matches on page")

            # 为每个已注册的 pair 查找匹配的比赛
            updated_count = 0
            for pair_id, pair_info in self._pair_info.items():
                registered_home = pair_info["home_team"].lower().strip()
                registered_away = pair_info["away_team"].lower().strip()

                # 在页面数据中查找匹配的比赛
                for match in matches_data:
                    page_home = match["home_team"].lower().strip()
                    page_away = match["away_team"].lower().strip()

                    # 简单匹配：检查队名是否相同
                    if registered_home == page_home and registered_away == page_away:
                        new_market_id = match["market_id"]

                        # 比赛状态默认为 False（未开始），将由 WebSocket inplay 字段更新
                        if "is_live" not in pair_info:
                            pair_info["is_live"] = False

                        self._log.info(
                            f"Found match for {pair_id}: {match['home_team']} vs {match['away_team']} "
                            f"-> market_id={new_market_id}"
                        )

                        # 清除旧的 selection_mapping 中该 pair 的条目
                        keys_to_remove = [
                            k for k, v in self._selection_mapping.items()
                            if v["pair_id"] == pair_id
                        ]
                        for k in keys_to_remove:
                            del self._selection_mapping[k]

                        # 更新 pair_info 中的 market_id 和 selections
                        pair_info["market_id"] = new_market_id
                        pair_info["selections"] = {}

                        # 重新注册新的 market_id:selection_id
                        if match["home_selection_id"]:
                            new_key = f"{new_market_id}:{match['home_selection_id']}"
                            self._selection_mapping[new_key] = {
                                "pair_id": pair_id,
                                "market_type": "home",
                            }
                            pair_info["selections"]["home"] = match["home_selection_id"]
                            self._log.debug(f"Re-registered: {new_key} -> {pair_id}/home")

                        if match["draw_selection_id"]:
                            new_key = f"{new_market_id}:{match['draw_selection_id']}"
                            self._selection_mapping[new_key] = {
                                "pair_id": pair_id,
                                "market_type": "draw",
                            }
                            pair_info["selections"]["draw"] = match["draw_selection_id"]
                            self._log.debug(f"Re-registered: {new_key} -> {pair_id}/draw")

                        if match["away_selection_id"]:
                            new_key = f"{new_market_id}:{match['away_selection_id']}"
                            self._selection_mapping[new_key] = {
                                "pair_id": pair_id,
                                "market_type": "away",
                            }
                            pair_info["selections"]["away"] = match["away_selection_id"]
                            self._log.debug(f"Re-registered: {new_key} -> {pair_id}/away")

                        updated_count += 1
                        break

            self._log.info(f"Updated selection mapping for {updated_count} pairs")

        except Exception as e:
            self._log.error(f"Error refreshing selection mapping: {e}")
            import traceback
            traceback.print_exc()

    async def _staleness_monitor_loop(self) -> None:
        """
        超时监控循环：定期检查 WebSocket 数据是否过时

        如果在配置的超时时间内没有收到任何赔率更新，则刷新页面。
        完全依赖 CDP WebSocket 拦截获取数据。
        """
        timeout_sec = self.config.orbitexch_staleness_timeout_sec
        self._log.info(
            f"Starting staleness monitor (check interval={self._staleness_check_interval}s, "
            f"timeout={timeout_sec}s)"
        )

        check_count = 0
        while self._running:
            try:
                await asyncio.sleep(self._staleness_check_interval)
                check_count += 1

                # 检查数据是否过时
                await self._check_and_refresh_if_stale()

                # 每 10 次检查模拟一次人类行为（约 5 分钟一次）
                if check_count % 10 == 0:
                    for page_key, page in list(self._pages.items()):
                        if page_key == "main":
                            continue
                        try:
                            await self._simulate_human_behavior(page)
                        except Exception:
                            pass

            except asyncio.CancelledError:
                self._log.info("Staleness monitor cancelled")
                break
            except Exception as e:
                self._log.error(f"Error in staleness monitor: {e}")
                await asyncio.sleep(5)

        self._log.info("Staleness monitor stopped")

    async def _simulate_human_behavior(self, page: Page) -> None:
        """
        模拟人类行为（鼠标移动、滚动等）

        防止被反爬虫系统检测为机器人
        """
        import random

        try:
            # 随机鼠标移动
            x = random.randint(100, 800)
            y = random.randint(100, 600)
            await page.mouse.move(x, y)

            # 随机小幅滚动
            scroll_delta = random.randint(-50, 50)
            await page.mouse.wheel(0, scroll_delta)

        except Exception:
            pass  # 忽略错误

    async def _check_and_refresh_if_stale(self) -> None:
        """
        按页面独立检查 WebSocket 数据是否过时，只刷新超时的页面
        """
        timeout_sec = self.config.orbitexch_staleness_timeout_sec
        now = time.time() * 1000

        for page_key, page in list(self._pages.items()):
            if page_key == "main":
                continue

            last_update = self._last_data_updates.get(page_key, 0)

            # 该页面从未收到过数据，跳过（初始化阶段）
            if last_update == 0:
                continue

            stale_seconds = (now - last_update) / 1000
            if stale_seconds <= timeout_sec:
                continue

            self._log.warning(
                f"Page {page_key} data is stale ({stale_seconds:.0f}s), refreshing..."
            )

            try:
                await self._refresh_single_page(page_key, page)
            except Exception as e:
                self._log.error(f"Error refreshing page {page_key}: {e}")

    async def _process_odds_update(self, data: dict) -> None:
        """
        处理赔率更新

        Args:
            data: {"marketId": str, "selectionId": str, "back": float, "lay": float, "timestamp": int}
        """
        market_id = str(data.get("marketId", ""))
        selection_id = str(data.get("selectionId", ""))
        back_price = data.get("back", 0)
        lay_price = data.get("lay", 0)
        timestamp = data.get("timestamp", int(time.time() * 1000))

        # 跳过没有价格的数据
        if not back_price and not lay_price:
            return

        # 使用复合键 market_id:selection_id 查找映射
        composite_key = f"{market_id}:{selection_id}"
        selection_info = self._selection_mapping.get(composite_key)

        # 调试日志：只在找到映射时显示
        if selection_info:
            self._log.debug(
                f"Odds update: {composite_key} -> back={back_price}, lay={lay_price}"
            )

        if selection_info:
            pair_id = selection_info["pair_id"]
            market_type = selection_info["market_type"]

            # 构造赔率数据
            odds_data = {
                "pair_id": pair_id,
                "market_id": market_id,
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
            # 未注册的 selection，记录用于调试
            if composite_key not in self._unmatched_selections:
                self._log.warning(
                    f"Unmatched selection: {composite_key} back={back_price} lay={lay_price} "
                    f"(mapping has {len(self._selection_mapping)} entries)"
                )
                self._unmatched_selections[composite_key] = {
                    "back": back_price,
                    "lay": lay_price,
                    "count": 1,
                }
            else:
                self._unmatched_selections[composite_key]["back"] = back_price
                self._unmatched_selections[composite_key]["lay"] = lay_price
                self._unmatched_selections[composite_key]["count"] += 1

    def _update_inplay_status(self, market_id: str, inplay: bool) -> None:
        """
        根据 WebSocket 数据更新比赛的 inplay 状态

        Args:
            market_id: 市场 ID
            inplay: 是否为赛中盘
        """
        # 查找该 market_id 对应的 pair_id
        pair_id = None
        for key, info in self._selection_mapping.items():
            if key.startswith(f"{market_id}:"):
                pair_id = info["pair_id"]
                break

        if pair_id and pair_id in self._pair_info:
            old_status = self._pair_info[pair_id].get("is_live")
            self._pair_info[pair_id]["is_live"] = inplay

            # 状态变化时记录日志
            if old_status is not None and old_status != inplay:
                status_str = "IN-PLAY" if inplay else "PRE-MATCH"
                self._log.info(
                    f"Match status changed: {pair_id} -> {status_str}"
                )
            elif old_status is None:
                status_str = "IN-PLAY" if inplay else "PRE-MATCH"
                self._log.debug(
                    f"Match status set: {pair_id} -> {status_str}"
                )

            if old_status is None or old_status != inplay:
                for callback in self._status_callbacks:
                    try:
                        callback(pair_id, inplay)
                    except Exception as e:
                        self._log.error(f"Status callback error for {pair_id}: {e}")

    # =========================================================================
    # Selection ID 映射管理
    # =========================================================================

    def clear_mappings(self) -> None:
        """
        清除所有映射数据（用于重新订阅前）
        """
        old_count = len(self._selection_mapping)
        self._selection_mapping.clear()
        self._pair_info.clear()
        self._latest_odds.clear()
        self._unmatched_selections.clear()
        self._log.info(f"Cleared {old_count} selection mappings")

    def register_status_callback(self, callback: Callable[[str, bool], None]) -> None:
        """注册比赛状态更新回调"""
        if callback in self._status_callbacks:
            return
        self._status_callbacks.append(callback)

    def register_pair_info(
        self,
        pair_id: str,
        home_team: str,
        away_team: str,
    ) -> None:
        """
        注册 pair 的队名信息（用于在订阅时通过队名重新匹配）

        Args:
            pair_id: 匹配的 pair ID
            home_team: 主队名
            away_team: 客队名
        """
        if pair_id not in self._pair_info:
            self._pair_info[pair_id] = {
                "home_team": home_team,
                "away_team": away_team,
                "selections": {},
            }
        self._log.info(f"Registered pair info: {pair_id} -> {home_team} vs {away_team}")

    def register_selection(
        self,
        market_id: str,
        selection_id: str,
        pair_id: str,
        market_type: str
    ) -> None:
        """
        注册 (market_id, selection_id) 到 pair_id 和 market_type 的映射

        Args:
            market_id: OrbitExch market ID (data-market-id)，唯一标识比赛
            selection_id: OrbitExch selection ID (data-selection-id)
            pair_id: 匹配的 pair ID
            market_type: 市场类型 (home/draw/away)
        """
        # 使用 market_id:selection_id 作为复合键
        key = f"{market_id}:{selection_id}"
        self._selection_mapping[key] = {
            "pair_id": pair_id,
            "market_type": market_type,
        }

        # 同时保存到 pair_info 的 selections 和 market_id 中
        if pair_id in self._pair_info:
            self._pair_info[pair_id]["selections"][market_type] = selection_id
            # 保存 market_id（所有 selections 共享同一个 market_id）
            self._pair_info[pair_id]["market_id"] = market_id

        self._log.info(f"Registered selection: {key} -> {pair_id}/{market_type}")

    def get_selection_info(self, market_id: str, selection_id: str) -> dict | None:
        """
        获取 (market_id, selection_id) 对应的 pair_id 和 market_type

        Args:
            market_id: OrbitExch market ID
            selection_id: OrbitExch selection ID

        Returns:
            {"pair_id": str, "market_type": str} 或 None
        """
        key = f"{market_id}:{selection_id}"
        return self._selection_mapping.get(key)

    def get_all_registered_selections(self) -> dict[str, dict]:
        """
        获取所有已注册的 selection 映射（用于调试）

        Returns:
            {composite_key: {"pair_id": str, "market_type": str}}
        """
        return self._selection_mapping.copy()

    def get_unmatched_selections(self) -> dict[str, dict]:
        """
        获取未匹配的 selection 记录（用于调试）

        Returns:
            {composite_key: {"back": float, "lay": float, "count": int}}
        """
        return self._unmatched_selections.copy()

    def get_pair_info(self) -> dict[str, dict]:
        """
        获取已注册的 pair 信息（用于调试）

        Returns:
            {pair_id: {"home_team": str, "away_team": str, "selections": dict}}
        """
        return self._pair_info.copy()

    def get_selection_mapping(self, pair_id: str) -> dict[int, str]:
        """
        获取指定 pair 的 selection_id -> outcome 映射

        Args:
            pair_id: 比赛 ID

        Returns:
            {selection_id: outcome}
            例: {39674645: "home", 39674646: "away", 39674647: "draw"}
        """
        result = {}
        for key, info in self._selection_mapping.items():
            if info.get("pair_id") == pair_id:
                # key 格式: "market_id:selection_id"
                parts = key.split(":")
                if len(parts) == 2:
                    try:
                        selection_id = int(parts[1])
                        result[selection_id] = info.get("market_type", "unknown")
                    except ValueError:
                        pass
        return result

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

    def on_page_refresh(self, callback: Callable[[], None]) -> None:
        """
        设置页面刷新回调（用于清除上层缓存）
        """
        self._page_refresh_callback = callback

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

    def get_match_status(self, pair_id: str) -> bool | None:
        """
        获取比赛状态（是否为赛中盘）

        Args:
            pair_id: 匹配的 pair ID

        Returns:
            True=赛中盘(in-play), False=赛前盘(pre-match), None=未知
        """
        pair_info = self._pair_info.get(pair_id)
        if pair_info:
            return pair_info.get("is_live")
        return None

    def get_all_match_statuses(self) -> dict[str, bool]:
        """
        获取所有比赛的状态

        Returns:
            {pair_id: is_live}
        """
        return {
            pair_id: info.get("is_live", False)
            for pair_id, info in self._pair_info.items()
            if "is_live" in info
        }

    # =========================================================================
    # 页面刷新
    # =========================================================================

    async def _refresh_single_page(self, page_key: str, page) -> None:
        """
        刷新单个页面：断开 CDP、reload、重建拦截

        Args:
            page_key: 页面标识
            page: Playwright Page 对象
        """
        self._log.info(f"Refreshing page {page_key}...")

        # 清除 OrbitExch 赔率缓存
        self._latest_odds.clear()
        if self._page_refresh_callback:
            try:
                self._page_refresh_callback()
            except Exception as e:
                self._log.warning(f"Page refresh callback error: {e}")

        # 断开旧的 CDP 会话
        old_cdp = self._cdp_sessions.pop(page_key, None)
        if old_cdp:
            try:
                await old_cdp.detach()
            except Exception:
                pass

        # 先设置 CDP 拦截（必须在 reload 之前，否则会错过 WebSocket 创建事件）
        await self._setup_websocket_interception(page, page_key)

        # 再刷新页面（reload 时创建的 WebSocket 会被 CDP 捕获）
        await page.reload(wait_until="networkidle", timeout=60000)
        await asyncio.sleep(2)

        # reload 后再次确保 Network.enable 生效（防止 reload 重置 CDP 状态）
        cdp = self._cdp_sessions.get(page_key)
        if cdp:
            try:
                await cdp.send("Network.enable")
                self._log.info(f"Re-enabled Network.enable after reload for {page_key}")
            except Exception as e:
                self._log.warning(f"Failed to re-enable Network after reload for {page_key}: {e}")
                # CDP 会话可能已失效，重新创建
                self._cdp_sessions.pop(page_key, None)
                await self._setup_websocket_interception(page, page_key)
                self._log.info(f"Recreated CDP session after reload for {page_key}")

        # 重新设置可见性欺骗
        await self._setup_visibility_spoof(page)

        # 重新抓取 selection 映射
        await self._refresh_selection_mapping_from_page(page)

        self._log.info(f"Page {page_key} refreshed")

    async def refresh_page(self, target_page_key: str | None = None) -> None:
        """
        刷新订阅页面

        Args:
            target_page_key: 指定刷新的页面，None 则刷新所有页面
        """
        for page_key, page in list(self._pages.items()):
            if page_key == "main":
                continue
            if target_page_key and page_key != target_page_key:
                continue

            try:
                await self._refresh_single_page(page_key, page)
            except Exception as e:
                self._log.error(f"Failed to refresh page {page_key}: {e}")

    async def close_main_page(self) -> None:
        """
        关闭主登录页面

        在所有 competition 订阅完成后调用，释放资源。
        登录会话已保存在浏览器上下文中，关闭主页面不影响其他页面。
        """
        main_page = self._pages.pop("main", None)
        if main_page:
            try:
                await main_page.close()
                self._log.info("Main page closed")
            except Exception as e:
                self._log.debug(f"Error closing main page: {e}")
        else:
            self._log.debug("Main page already closed or not exists")

    def get_cdp_status(self) -> dict:
        """
        获取 CDP WebSocket 拦截状态（用于调试）

        Returns:
            {
                "cdp_sessions": list of page_keys with active CDP sessions,
                "ws_connections": dict of tracked WebSocket connections,
                "last_data_updates": dict of per-page {last_data_update, age_sec}
            }
        """
        now = time.time() * 1000
        per_page = {}
        for page_key, ts in self._last_data_updates.items():
            per_page[page_key] = {
                "last_data_update": ts,
                "age_sec": (now - ts) / 1000 if ts else -1,
            }

        return {
            "cdp_sessions": list(self._cdp_sessions.keys()),
            "ws_connections": self._ws_request_ids.copy(),
            "last_data_updates": per_page,
        }
