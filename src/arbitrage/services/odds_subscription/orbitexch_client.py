"""
OrbitExch 赔率客户端

使用 Playwright + MutationObserver 监控 DOM 变化获取实时赔率数据。

关键特性：
- 直接访问 competition 页面（无需从首页导航）
- 使用 MutationObserver 监控赔率 DOM 变化
- 支持页面缩放以显示更多比赛
"""

import asyncio
import logging
import time
from typing import Callable

from playwright.async_api import async_playwright, Page, Browser


from .config import OddsSubscriptionConfig


class OrbitExchOddsClient:
    """
    OrbitExch 赔率客户端

    使用 Playwright + MutationObserver 监控 DOM 变化：
    1. 打开浏览器并登录 OrbitExch
    2. 直接导航到指定 competition 页面
    3. 注入 MutationObserver 监控赔率 DOM 变化
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

        # 回调函数
        self._price_update_callback: Callable[[dict], None] | None = None

        # 状态
        self._running = False

        # 超时监控任务（替代轮询）
        self._staleness_monitor_task: asyncio.Task | None = None
        self._staleness_check_interval = 10  # 每 10 秒检查一次数据是否过时

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

        # 停止超时监控任务
        if self._staleness_monitor_task and not self._staleness_monitor_task.done():
            self._staleness_monitor_task.cancel()
            try:
                await self._staleness_monitor_task
            except asyncio.CancelledError:
                pass

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

        直接导航到 competition 页面，无需从首页开始

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

        # 1. CDP 模式：优先查找已存在的匹配标签页
        page = None
        if self.config.orbitexch_cdp_url:
            for existing_page in self._context.pages:
                if f"/competition/{competition_id}" in existing_page.url:
                    page = existing_page
                    self._log.info(f"Found existing tab for competition {competition_id}")
                    break

        # 2. 如果没有找到现有标签页，创建新的
        if page is None:
            page = await self._context.new_page()
            self._log.info(f"Created new tab, navigating to: {url}")

        self._pages[page_key] = page

        # 4. 导航到 competition 页面
        await page.goto(url, wait_until="networkidle")

        # 5. 等待页面加载
        await asyncio.sleep(2)

        # 6. 注入可见性欺骗脚本（后备，因为 init_script 应该已经注入）
        await self._setup_visibility_spoof(page)

        # 7. 注入 WebSocket 监控脚本
        await self._setup_websocket_monitor(page)

        # 8. 重新抓取页面上的比赛并更新 selection 映射
        await self._refresh_selection_mapping_from_page(page)

        # 9. 设置 MutationObserver
        await self._expose_odds_callback(page)
        await self._setup_mutation_observer(page, page_key)

        # 10. 启动超时监控任务（检查数据是否过时，过时则刷新页面）
        if self._staleness_monitor_task is None or self._staleness_monitor_task.done():
            self._staleness_monitor_task = asyncio.create_task(self._staleness_monitor_loop())
            self._log.info("Started staleness monitor task")

        # 11. 记录订阅的 events
        for event_id in event_ids:
            self._subscribed_events[event_id] = {
                "sport_id": sport_id,
                "competition_id": competition_id,
            }

        self._log.info(f"Subscribed to {len(event_ids)} events in competition {competition_id}")
        self._log.info(f"Total open tabs: {len(self._pages)}")

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

    async def _setup_websocket_monitor(self, page: Page) -> None:
        """
        注入 WebSocket 监控和分析脚本

        监控 WebSocket 连接状态，并分析消息格式以了解订阅机制
        """
        await page.evaluate("""
            () => {
                if (window.__wsMonitorInstalled) return;
                window.__wsMonitorInstalled = true;

                // 记录上次数据更新时间
                window.__lastDataUpdate = Date.now();

                // 保存所有 WebSocket 实例，便于分析
                window.__webSockets = [];

                // 拦截 WebSocket 以监控和分析
                const OriginalWebSocket = window.WebSocket;
                window.WebSocket = function(url, protocols) {
                    const ws = new OriginalWebSocket(url, protocols);
                    window.__webSockets.push(ws);

                    // 保存原始 send 方法
                    const originalSend = ws.send.bind(ws);

                    // 拦截 send 方法以分析发送的消息
                    ws.send = function(data) {
                        try {
                            let parsed = data;
                            if (typeof data === 'string') {
                                try {
                                    parsed = JSON.parse(data);
                                } catch(e) {}
                            }
                            console.log('[WS Monitor] Sending:', parsed);

                            // 保存发送的消息用于分析
                            if (!window.__wsSentMessages) window.__wsSentMessages = [];
                            window.__wsSentMessages.push({
                                timestamp: Date.now(),
                                data: parsed
                            });
                        } catch(e) {}

                        return originalSend(data);
                    };

                    ws.addEventListener('open', () => {
                        console.log('[WS Monitor] WebSocket connected:', url);
                        window.__wsConnected = true;
                        window.__wsUrl = url;
                    });

                    ws.addEventListener('close', (event) => {
                        console.log('[WS Monitor] WebSocket closed:', event.code, event.reason);
                        window.__wsConnected = false;
                    });

                    ws.addEventListener('message', (event) => {
                        window.__lastDataUpdate = Date.now();

                        // 分析接收的消息（只记录前几条用于调试）
                        if (!window.__wsReceivedMessages) window.__wsReceivedMessages = [];
                        if (window.__wsReceivedMessages.length < 50) {
                            try {
                                let parsed = event.data;
                                if (typeof event.data === 'string') {
                                    try {
                                        parsed = JSON.parse(event.data);
                                    } catch(e) {}
                                }
                                window.__wsReceivedMessages.push({
                                    timestamp: Date.now(),
                                    data: parsed
                                });
                            } catch(e) {}
                        }
                    });

                    ws.addEventListener('error', (error) => {
                        console.log('[WS Monitor] WebSocket error:', error);
                    });

                    return ws;
                };
                window.WebSocket.prototype = OriginalWebSocket.prototype;
                window.WebSocket.CONNECTING = OriginalWebSocket.CONNECTING;
                window.WebSocket.OPEN = OriginalWebSocket.OPEN;
                window.WebSocket.CLOSING = OriginalWebSocket.CLOSING;
                window.WebSocket.CLOSED = OriginalWebSocket.CLOSED;

                console.log('[WS Monitor] WebSocket monitor installed');
            }
        """)
        self._log.info("WebSocket monitor installed")

    async def _refresh_selection_mapping_from_page(self, page: Page) -> None:
        """
        从当前页面重新抓取比赛信息，并通过队名匹配更新 selection_mapping

        这是必要的，因为 market_id 可能在 discovery 和 subscription 之间发生变化
        （页面上的比赛排序、新增/删除等）
        """
        self._log.info("Refreshing selection mapping from live page...")

        try:
            # 抓取页面上所有比赛的信息
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

                        # 重新注册新的 market_id:selection_id
                        if match["home_selection_id"]:
                            new_key = f"{new_market_id}:{match['home_selection_id']}"
                            self._selection_mapping[new_key] = {
                                "pair_id": pair_id,
                                "market_type": "home",
                            }
                            self._log.info(f"Re-registered: {new_key} -> {pair_id}/home")

                        if match["draw_selection_id"]:
                            new_key = f"{new_market_id}:{match['draw_selection_id']}"
                            self._selection_mapping[new_key] = {
                                "pair_id": pair_id,
                                "market_type": "draw",
                            }
                            self._log.info(f"Re-registered: {new_key} -> {pair_id}/draw")

                        if match["away_selection_id"]:
                            new_key = f"{new_market_id}:{match['away_selection_id']}"
                            self._selection_mapping[new_key] = {
                                "pair_id": pair_id,
                                "market_type": "away",
                            }
                            self._log.info(f"Re-registered: {new_key} -> {pair_id}/away")

                        updated_count += 1
                        break

            self._log.info(f"Updated selection mapping for {updated_count} pairs")

        except Exception as e:
            self._log.error(f"Error refreshing selection mapping: {e}")
            import traceback
            traceback.print_exc()

    async def _expose_odds_callback(self, page: Page) -> None:
        """
        暴露 Python 回调函数给 JavaScript

        这样 MutationObserver 可以直接调用 Python 函数，无需轮询
        """
        # 检查是否已经暴露过
        already_exposed = await page.evaluate("() => typeof window.__onOrbitOddsChange === 'function'")
        if already_exposed:
            self._log.debug("Callback already exposed, skipping")
            return

        async def on_odds_change(market_id: str, selection_id: str, back: float, lay: float, timestamp: int):
            """JavaScript 调用的回调函数"""
            await self._process_odds_update({
                "marketId": market_id,
                "selectionId": selection_id,
                "back": back,
                "lay": lay,
                "timestamp": timestamp,
            })

        # 暴露函数给 JavaScript
        try:
            await page.expose_function("__onOrbitOddsChange", on_odds_change)
            self._log.info("Exposed __onOrbitOddsChange callback to JavaScript")
        except Exception as e:
            # 可能函数已经存在
            self._log.debug(f"Could not expose function (may already exist): {e}")

    async def _setup_mutation_observer(self, page: Page, page_key: str) -> None:
        """
        注入 MutationObserver 脚本监控赔率 DOM 变化

        DOM 结构：
        - div.betContentContainer[data-selection-id] 包含每个选项的赔率
        - .biab_back-cell .biab_bet-odds 包含 back 价格
        - .biab_lay-cell .biab_bet-odds 包含 lay 价格

        当赔率变化时，直接调用暴露的 Python 回调函数
        """
        self._log.info(f"Setting up MutationObserver for {page_key}...")

        # 注入监控脚本
        await page.evaluate("""
            () => {
                // 防止重复安装
                if (window.__orbitObserverInstalled) return;
                window.__orbitObserverInstalled = true;

                // 去重：记录最近发送的赔率，避免重复触发
                const lastSent = new Map();

                // 创建 MutationObserver
                const observer = new MutationObserver((mutations) => {
                    mutations.forEach(mutation => {
                        // 检查是否是赔率文本变化
                        if (mutation.type === 'characterData' || mutation.type === 'childList') {
                            const target = mutation.target;

                            // 向上查找 selection 容器
                            const container = target.closest?.('[data-selection-id]') ||
                                             target.parentElement?.closest?.('[data-selection-id]');

                            if (container) {
                                const selectionId = container.getAttribute('data-selection-id');

                                // 查找 market_id（向上查找包含 data-market-id 的元素）
                                const marketContainer = container.closest('[data-market-id]');
                                const marketId = marketContainer?.getAttribute('data-market-id') || '';

                                // 提取 back 和 lay 价格
                                const backEl = container.querySelector('.biab_back-cell .biab_bet-odds, .back-cell .biab_bet-odds');
                                const layEl = container.querySelector('.biab_lay-cell .biab_bet-odds, .lay-cell .biab_bet-odds');

                                const backPrice = parseFloat(backEl?.textContent?.trim()) || 0;
                                const layPrice = parseFloat(layEl?.textContent?.trim()) || 0;

                                // 调试日志
                                console.log('[MutationObserver] Detected change:', {
                                    selectionId, marketId, backPrice, layPrice,
                                    hasMarketId: !!marketId,
                                    targetNodeType: target.nodeType,
                                    targetNodeName: target.nodeName
                                });

                                if (selectionId && marketId && (backPrice || layPrice)) {
                                    // 去重检查（使用 market_id:selection_id 作为键）
                                    const key = `${marketId}:${selectionId}_${backPrice}_${layPrice}`;
                                    const now = Date.now();
                                    const lastTime = lastSent.get(key);

                                    // 同样的赔率 500ms 内不重复发送
                                    if (!lastTime || now - lastTime > 500) {
                                        lastSent.set(key, now);

                                        // 更新数据更新时间戳（供超时监控使用）
                                        window.__lastDataUpdate = now;

                                        console.log('[MutationObserver] Calling Python callback:', marketId, selectionId, backPrice, layPrice);

                                        // 直接调用 Python 回调（传入 marketId）
                                        window.__onOrbitOddsChange(marketId, selectionId, backPrice, layPrice, now);
                                    }
                                } else {
                                    console.log('[MutationObserver] Skipped - missing data:', {selectionId, marketId, backPrice, layPrice});
                                }
                            }
                        }
                    });
                });

                // 监控整个文档
                observer.observe(document.body, {
                    childList: true,
                    subtree: true,
                    characterData: true,
                    characterDataOldValue: true
                });

                console.log('OrbitExch MutationObserver installed (callback mode)');
            }
        """)

        # 做一次初始抓取
        await self._scrape_current_odds(page)

        self._log.info(f"MutationObserver setup complete for {page_key}")

    async def _scrape_current_odds(self, page: Page) -> None:
        """
        抓取当前页面上的所有赔率

        Args:
            page: 浏览器页面
        """
        # 静默抓取，不输出日志（轮询模式下会频繁调用）

        try:
            odds_data = await page.evaluate("""
                () => {
                    const results = [];

                    // 查找所有 selection 容器
                    const containers = document.querySelectorAll('[data-selection-id]');

                    containers.forEach(container => {
                        const selectionId = container.getAttribute('data-selection-id');

                        // 查找 market_id（向上查找包含 data-market-id 的元素）
                        const marketContainer = container.closest('[data-market-id]');
                        const marketId = marketContainer?.getAttribute('data-market-id') || '';

                        // 提取 back 和 lay 价格
                        const backEl = container.querySelector('.biab_back-cell .biab_bet-odds, .back-cell .biab_bet-odds');
                        const layEl = container.querySelector('.biab_lay-cell .biab_bet-odds, .lay-cell .biab_bet-odds');

                        const backPrice = backEl?.textContent?.trim() || '';
                        const layPrice = layEl?.textContent?.trim() || '';

                        if (selectionId && marketId && (backPrice || layPrice)) {
                            results.push({
                                marketId,
                                selectionId,
                                back: parseFloat(backPrice) || 0,
                                lay: parseFloat(layPrice) || 0,
                                timestamp: Date.now()
                            });
                        }
                    });

                    return results;
                }
            """)

            self._log.debug(f"Scraped {len(odds_data)} odds entries")

            # 处理抓取的赔率
            for item in odds_data:
                await self._process_odds_update(item)

        except Exception as e:
            self._log.error(f"Error scraping odds: {e}")

    async def _staleness_monitor_loop(self) -> None:
        """
        超时监控循环：定期检查页面数据是否过时

        如果页面在配置的超时时间内没有收到任何赔率更新，则刷新页面。
        不进行轮询抓取数据，完全依赖 MutationObserver。
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

                # 遍历所有订阅的页面
                for page_key, page in list(self._pages.items()):
                    if page_key == "main":
                        continue  # 跳过主登录页

                    try:
                        # 检查页面数据是否过时
                        await self._check_and_refresh_if_stale(page, page_key)

                        # 每 30 次检查模拟一次人类行为（约 5 分钟一次）
                        if check_count % 30 == 0:
                            await self._simulate_human_behavior(page)

                    except Exception as e:
                        self._log.debug(f"Error checking page {page_key}: {e}")

            except asyncio.CancelledError:
                self._log.info("Staleness monitor cancelled")
                break
            except Exception as e:
                self._log.error(f"Error in staleness monitor: {e}")
                await asyncio.sleep(5)  # 出错后等待一会再继续

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

    async def _reinstall_mutation_observer(self, page: Page, page_key: str) -> None:
        """
        重新安装 MutationObserver（确保始终有效）

        先断开旧的 observer，再创建新的
        """
        try:
            # 断开旧的 observer 并重新安装
            await page.evaluate("""
                () => {
                    // 断开旧的 observer
                    if (window.__orbitObserver) {
                        window.__orbitObserver.disconnect();
                    }
                    // 重置安装标志，允许重新安装
                    window.__orbitObserverInstalled = false;
                }
            """)

            # 重新设置 MutationObserver
            await self._setup_mutation_observer(page, page_key)

        except Exception as e:
            self._log.debug(f"Error reinstalling MutationObserver: {e}")

    async def _check_and_refresh_if_stale(self, page: Page, page_key: str) -> None:
        """
        检查页面数据是否过时，如果超过配置的超时时间无更新则刷新页面
        """
        timeout_sec = self.config.orbitexch_staleness_timeout_sec

        try:
            # 获取上次数据更新时间
            last_update = await page.evaluate("() => window.__lastDataUpdate || 0")
            now = await page.evaluate("() => Date.now()")

            stale_seconds = (now - last_update) / 1000

            if stale_seconds > timeout_sec:
                self._log.warning(
                    f"Page {page_key} data is stale ({stale_seconds:.0f}s), refreshing..."
                )
                await page.reload(wait_until="networkidle")
                await asyncio.sleep(2)

                # 重新设置监控
                await self._setup_websocket_monitor(page)
                await self._expose_odds_callback(page)
                await self._setup_mutation_observer(page, page_key)

                self._log.info(f"Page {page_key} refreshed")

        except Exception as e:
            self._log.debug(f"Error checking staleness for {page_key}: {e}")

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
                self._unmatched_selections[composite_key] = {
                    "back": back_price,
                    "lay": lay_price,
                    "count": 1,
                }
            else:
                self._unmatched_selections[composite_key]["back"] = back_price
                self._unmatched_selections[composite_key]["lay"] = lay_price
                self._unmatched_selections[composite_key]["count"] += 1

    # =========================================================================
    # Selection ID 映射管理
    # =========================================================================

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

        # 同时保存到 pair_info 的 selections 中
        if pair_id in self._pair_info:
            self._pair_info[pair_id]["selections"][market_type] = selection_id

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

                # 重新暴露回调函数并设置 MutationObserver
                await self._expose_odds_callback(page)
                await self._setup_mutation_observer(page, page_key)

                self._log.info(f"Page {page_key} refreshed")
            except Exception as e:
                self._log.error(f"Failed to refresh page {page_key}: {e}")

        self._log.info("All pages refreshed")

    async def get_websocket_analysis(self) -> dict:
        """
        获取 WebSocket 消息分析（用于调试）

        返回捕获的 WebSocket 发送和接收的消息，帮助分析订阅机制

        Returns:
            {
                "sent_messages": [...],
                "received_messages": [...],
                "ws_url": str,
                "ws_connected": bool
            }
        """
        result = {
            "sent_messages": [],
            "received_messages": [],
            "ws_url": "",
            "ws_connected": False,
            "pages": {}
        }

        for page_key, page in self._pages.items():
            if page_key == "main":
                continue

            try:
                page_data = await page.evaluate("""
                    () => {
                        return {
                            sent_messages: window.__wsSentMessages || [],
                            received_messages: window.__wsReceivedMessages || [],
                            ws_url: window.__wsUrl || '',
                            ws_connected: window.__wsConnected || false,
                            last_data_update: window.__lastDataUpdate || 0
                        };
                    }
                """)
                result["pages"][page_key] = page_data

                # 汇总
                if page_data["ws_url"]:
                    result["ws_url"] = page_data["ws_url"]
                if page_data["ws_connected"]:
                    result["ws_connected"] = True
                result["sent_messages"].extend(page_data["sent_messages"])
                result["received_messages"].extend(page_data["received_messages"])

            except Exception as e:
                self._log.debug(f"Error getting WS analysis for {page_key}: {e}")

        return result
