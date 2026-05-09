"""
风控服务

负责止损检查和持仓风险管理。
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from .config import RiskConfig
from .position import PositionManager, MatchPosition
from src.arbitrage.services.execution.messages import SessionCompleteMessage
from src.arbitrage.services.execution.topics import SESSION_COMPLETE_TOPIC_PATTERN
from src.arbitrage.services.execution.cleanup import PostSessionCleanup
from nautilus_trader.adapters.polymarket.contract import PolymarketContractService
from src.arbitrage.services.execution.config import ExecutionConfig
from src.arbitrage.services.odds_subscription.config import OddsSubscriptionConfig
from src.arbitrage.services.odds_subscription.messages import PairActivityMessage
from src.arbitrage.services.odds_subscription.topics import pair_activity_topic


@dataclass
class RiskCheckResult:
    """
    风控检查结果

    Attributes:
        allowed: 是否允许下注
        reason: 拒绝原因（如果不允许）
        match_blocked: 是否因单场止损被阻止
        global_blocked: 是否因全局止损被阻止
        tp_blocked: 是否因止盈被阻止
        way_rebate: 该比赛的各方向返水率
        min_way_rebate: 该比赛的最低返水率
        global_min_sum: 全局最低返水率之和
    """
    allowed: bool
    reason: str = ""
    match_blocked: bool = False
    global_blocked: bool = False
    tp_blocked: bool = False
    way_rebate: dict[str, float] | None = None
    min_way_rebate: float | None = None
    global_min_sum: float | None = None

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "match_blocked": self.match_blocked,
            "global_blocked": self.global_blocked,
            "tp_blocked": self.tp_blocked,
            "way_rebate": self.way_rebate,
            "min_way_rebate": self.min_way_rebate,
            "global_min_sum": self.global_min_sum,
        }


class RiskService:
    """
    风控服务

    功能：
    1. 跟踪各比赛的持仓
    2. 计算各方向的持仓返水率
    3. 提供止损检查
    4. 阻止触发止损的比赛继续下注
    """

    def __init__(
        self,
        config: RiskConfig | None = None,
        logger: logging.Logger | None = None,
        share: float = 100.0,
    ):
        self._config = config or RiskConfig()
        self._log = logger or logging.getLogger(self.__class__.__name__)
        self._position_manager = PositionManager(default_share=share)
        self._fx = 1.0

        # 余额跟踪
        self._pm_balance: float = 0.0  # Polymarket USDC 余额
        self._oe_balance: float = 0.0  # OrbitExch 余额

        # 消息总线和外部服务
        self._msgbus = None
        self._odds_service = None

        # 健康检查
        self._pm_healthy: bool = False
        self._oe_healthy: bool = False
        self._rebate_healthy: bool = False
        self._health_ok: bool = False
        self._health_check_task: asyncio.Task | None = None
        # 触发逻辑：阻塞标志 + 下次执行时间 + 主动触发事件
        self._health_check_blocked: bool = False
        self._next_health_check_at: float | None = None
        self._health_check_event: asyncio.Event | None = None
        self._execution_service = None  # 用于访问 PM client 和 OE pages

        # Cleanup 组件
        self._cleanup: PostSessionCleanup | None = None

    @property
    def config(self) -> RiskConfig:
        return self._config

    def update_config(self, config: RiskConfig) -> None:
        """更新配置"""
        self._config = config
        self._log.info(f"Risk config updated: match_sl={config.match_sl}, global_sl={config.global_sl}")

    def set_share(self, share: float) -> None:
        """设置 share 参数"""
        self._position_manager.set_share(share)
        self._log.info(f"Share updated: {share}")

    def set_fx(self, fx: float) -> None:
        """设置汇率参数"""
        self._fx = fx
        self._position_manager.set_fx(fx)
        self._log.info(f"FX rate updated: {fx}")

    def set_msgbus(self, msgbus) -> None:
        """设置消息总线并订阅主题"""
        if self._msgbus is msgbus:
            return
        self._msgbus = msgbus
        if not self._msgbus:
            return
        self._msgbus.subscribe(SESSION_COMPLETE_TOPIC_PATTERN, self._on_session_complete_message)
        self._log.info("Subscribed to session_complete messages")

    def set_odds_service(self, odds_service) -> None:
        """设置赔率服务引用（用于查询持仓数据）"""
        self._odds_service = odds_service
        self._log.info("Odds service reference set")

    def set_execution_service(self, execution_service) -> None:
        """设置执行服务引用（用于健康检查访问 PM client 和 OE pages）"""
        self._execution_service = execution_service
        self._log.info("Execution service reference set for health check")

    def update_orbitexch_balance(self, balance: float) -> None:
        """
        更新 OrbitExch 余额（由 OddsService 通过回调触发）

        Args:
            balance: OrbitExch 余额
        """
        old_balance = self._oe_balance
        self._oe_balance = balance
        if abs(balance - old_balance) > 0.01:
            self._log.info(
                f"OrbitExch balance updated: {old_balance:.2f} -> {balance:.2f}"
            )

    def get_balances(self) -> dict[str, float]:
        """
        获取当前余额

        Returns:
            {"polymarket": float, "orbitexch": float}
        """
        return {
            "polymarket": self._pm_balance,
            "orbitexch": self._oe_balance,
        }

    def start_health_check_loop(self, interval: float | None = None) -> None:
        """启动后台健康检查循环"""
        if self._health_check_task and not self._health_check_task.done():
            self._log.warning("Health check loop already running")
            return

        interval = interval or self._config.health_check_interval_sec
        try:
            loop = asyncio.get_running_loop()
            self._health_check_task = loop.create_task(self._health_check_loop(interval))
            self._log.info(f"Health check loop started (interval={interval}s)")
        except RuntimeError:
            self._log.warning("No running event loop, health check loop not started")

    async def run_initial_health_check(self) -> None:
        """
        立即同步跑一次健康检查（启动时使用）。

        新设计中 ``subscribe_event`` / ``subscribe_competition`` 只 wire WS / 记账，
        不再触发任何 IO；首次开页（OE）和首次 fetch（PM）由健康检查驱动。
        系统在"准备就绪"后应该调用本方法等第一轮数据就位，再启动 ``start_health_check_loop``
        进入 120s 一次的常规节奏。
        """
        self._log.info("Running initial health check")
        await self._run_health_check()
        self._log.info(
            f"Initial health check complete: pm={'OK' if self._pm_healthy else 'FAIL'}, "
            f"oe={'OK' if self._oe_healthy else 'FAIL'}, "
            f"rebate={'OK' if self._rebate_healthy else 'FAIL'}"
        )

    def block_health_check(self) -> None:
        """阻塞健康检查：下一次循环唤醒时检测到 blocked 则跳过执行体（不改变检查结果）。"""
        if not self._health_check_blocked:
            self._health_check_blocked = True
            self._log.info("Health check blocked")

    def unblock_health_check(self) -> None:
        """放通健康检查（标志位翻转，循环节奏不变）。"""
        if self._health_check_blocked:
            self._health_check_blocked = False
            self._log.info("Health check unblocked")

    def is_health_check_blocked(self) -> bool:
        return self._health_check_blocked

    def trigger_health_check(self) -> None:
        """主动触发一次健康检查：立即唤醒循环（仍受 block 标志拦截）。"""
        if self._health_check_event is not None:
            self._health_check_event.set()

    def get_next_health_check_at(self) -> float | None:
        """下次健康检查的预定 monotonic 时间（None = 正在执行 / 未规划）。"""
        return self._next_health_check_at

    async def _health_check_loop(self, interval: float) -> None:
        """
        后台健康检查循环（schedule + event-driven）。

        - 每轮等到 ``_next_health_check_at`` 或被 ``trigger_health_check`` 唤醒
        - 唤醒后先把 ``_next_health_check_at`` 置空，再判 ``_health_check_blocked``：
          被阻塞 → 跳过执行体不改变结果；放通 → 跑 ``_run_health_check``
        - 不论 blocked / 异常，每轮结束前用当前 ``health_check_interval_sec`` 规划下次时间
        """
        if self._health_check_event is None:
            self._health_check_event = asyncio.Event()

        # 初次进入循环：先按 interval 规划首次唤醒（initial check 已在 run_initial_health_check 跑过）
        self._next_health_check_at = time.monotonic() + self._config.health_check_interval_sec

        while True:
            # --- 等到下次时间或被主动触发 ---
            try:
                target = self._next_health_check_at
                wait_for = max(0.0, target - time.monotonic()) if target is not None else 0.0
                if wait_for > 0:
                    try:
                        await asyncio.wait_for(
                            self._health_check_event.wait(), timeout=wait_for
                        )
                    except asyncio.TimeoutError:
                        pass
                self._health_check_event.clear()
            except asyncio.CancelledError:
                self._log.info("Health check loop cancelled")
                break

            # --- 执行前：置空下次时间 + 检查 block 标志 ---
            self._next_health_check_at = None
            try:
                if self._health_check_blocked:
                    self._log.debug(
                        "Health check blocked, skipping body (results unchanged)"
                    )
                else:
                    await self._run_health_check()
            except asyncio.CancelledError:
                self._log.info("Health check loop cancelled")
                break
            except Exception as e:
                self._log.error(f"Health check loop error: {e}")

            # --- 每轮结束前：按当前配置规划下次时间（支持配置动态更新） ---
            self._next_health_check_at = (
                time.monotonic() + self._config.health_check_interval_sec
            )

    async def _run_health_check(self) -> None:
        """
        执行一次健康检查。

        - **Polymarket**：每轮无条件主动 fetch（PM Data API 偶发宕机，必须每次重拉）。
          ``get_ok`` + balance + ``fetch_positions`` + ``fetch_open_orders`` 全部成功才算 OK。
        - **OrbitExch**：当前不健康 → ``refresh_page`` 走 adapter 内部的"首次打开 / 刷新"统一路径；
          当前健康 → 轻量探测（``/customer/api/currentBets``）确认 session 仍有效。
          失败时尝试一次"重新登录 + refresh"作为兜底。
        - **Rebate**：从已 fetch 的数据计算 way_rebate（不再触发 IO）。
        """
        # 有活跃 pair 时跳过（execution 或 risk 正在操作）
        if self._odds_service and self._odds_service._pair_activity:
            active_pairs = [
                pid for pid in self._odds_service._pair_activity
                if self._odds_service._is_pair_active(pid)
            ]
            if active_pairs:
                self._log.debug(f"Skipping health check: active pairs {active_pairs}")
                return

        old_ok = self._health_ok

        pm_ok = await self._check_polymarket_health()
        oe_ok = await self._check_orbitexch_health()
        rebate_ok = self._compute_rebates_safe()

        # --- 更新状态 ---
        self._pm_healthy = pm_ok
        self._oe_healthy = oe_ok
        self._rebate_healthy = rebate_ok
        self._health_ok = pm_ok and oe_ok and rebate_ok

        # 状态变化时记录日志
        if self._health_ok != old_ok:
            if self._health_ok:
                self._log.info(
                    f"Health check recovered: pm=OK, oe=OK, rebate=OK, "
                    f"pm_balance={self._pm_balance:.2f}, oe_balance={self._oe_balance:.2f}"
                )
            else:
                self._log.warning(
                    f"Health check FAILED: pm={'OK' if pm_ok else 'FAIL'}, "
                    f"oe={'OK' if oe_ok else 'FAIL'}, "
                    f"rebate={'OK' if rebate_ok else 'FAIL'}, "
                    f"pm_balance={self._pm_balance:.2f}, oe_balance={self._oe_balance:.2f}"
                )

    async def _check_polymarket_health(self) -> bool:
        """PM 每轮主动拉一次：get_ok + balance + fetch_positions + fetch_open_orders。"""
        pm_client = None
        if self._odds_service:
            pm_client = self._odds_service.get_polymarket_client()
        if not pm_client or not pm_client._clob_client:
            self._log.debug("Polymarket client not available, skipping health check")
            return False

        try:
            clob_client = pm_client._clob_client

            # 通过 PolymarketClient 的统一锁序列化
            await pm_client._call_api(clob_client.get_ok)

            # 余额
            from py_clob_client_v2 import BalanceAllowanceParams, AssetType
            from nautilus_trader.adapters.polymarket.common.conversion import usdce_from_units

            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=2,
            )
            response = await pm_client._call_api(
                clob_client.get_balance_allowance, params
            )
            balance_raw = int(response.get("balance", 0))
            balance_usdc = usdce_from_units(balance_raw).as_double()
            old_balance = self._pm_balance
            self._pm_balance = balance_usdc
            if abs(balance_usdc - old_balance) > 0.01:
                self._log.info(
                    f"Polymarket balance updated: {old_balance:.2f} -> {balance_usdc:.2f}"
                )

            # 主动拉仓位 + 挂单（不论当前 health 状态都做）
            positions = await pm_client.fetch_positions()
            if positions is None:
                self._log.warning("Polymarket fetch_positions returned None")
                return False
            await pm_client.fetch_open_orders()

            return True
        except Exception as e:
            self._log.warning(f"Polymarket health check failed: {e}")
            return False

    async def _check_orbitexch_health(self) -> bool:
        """
        OE 健康检查驱动 adapter 的"首次打开 / 刷新"统一路径。

        - 不健康 → ``refresh_page``（adapter 内部按需 goto / reload）
        - 健康   → 轻量探测 ``/customer/api/currentBets``
        - 任一失败 → 尝试 re-login + refresh 一次兜底
        """
        oe_client = None
        if self._odds_service:
            oe_client = self._odds_service.get_orbitexch_client()
        if not oe_client or not oe_client._context:
            self._log.debug("OrbitExch client not available, skipping health check")
            return False

        if not oe_client._subscribed_competitions:
            self._log.debug("No OrbitExch subscribed competitions, marking unhealthy")
            return False

        if self._oe_healthy:
            oe_ok = await self._oe_lightweight_probe(oe_client)
        else:
            oe_ok = await self._oe_drive_refresh(oe_client)

        if not oe_ok:
            if await self._oe_relogin(oe_client):
                oe_ok = await self._oe_drive_refresh(oe_client)

        return oe_ok

    async def _oe_lightweight_probe(self, oe_client) -> bool:
        """从任一 competition page 用 page.evaluate 探测 ``/currentBets``，确认 session 有效。"""
        for page_key, page in list(oe_client._pages.items()):
            if page_key == "main":
                continue
            try:
                result = await page.evaluate(
                    """async () => {
                        try {
                            const cookies = document.cookie.split(';');
                            let csrfToken = '';
                            for (const cookie of cookies) {
                                const [name, value] = cookie.trim().split('=');
                                if (name === 'CSRF-TOKEN') {
                                    csrfToken = decodeURIComponent(value);
                                    break;
                                }
                            }
                            if (!csrfToken) return { ok: false, reason: 'no_csrf' };
                            const response = await fetch('/customer/api/currentBets', {
                                method: 'GET',
                                headers: {
                                    'Accept': 'application/json',
                                    'x-csrf-token': csrfToken,
                                },
                                credentials: 'include',
                            });
                            return { ok: response.ok, status: response.status };
                        } catch (error) {
                            return { ok: false, reason: error.message };
                        }
                    }"""
                )
                if result and result.get("ok"):
                    return True
                self._log.warning(
                    f"OrbitExch probe failed (page={page_key}): {result}"
                )
            except Exception as e:
                self._log.warning(
                    f"OrbitExch probe failed (page={page_key}): {e}"
                )
        return False

    async def _oe_drive_refresh(self, oe_client) -> bool:
        """触发 ``oe_client.refresh_page`` 并把新创建的 competition page 同步给 executor。"""
        try:
            ok = await oe_client.refresh_page(
                timeout=self._config.tracking_timeout_sec
                if hasattr(self._config, "tracking_timeout_sec")
                else 30.0,
            )
        except Exception as e:
            self._log.warning(f"OrbitExch refresh_page failed: {e}")
            return False
        if ok:
            self._sync_oe_executor_pages(oe_client)
        return ok

    async def _oe_relogin(self, oe_client) -> bool:
        """临时页面重新登录，刷新 context 中的 session cookie。"""
        if not oe_client.config.orbitexch_username or not oe_client.config.orbitexch_password:
            self._log.debug("No OrbitExch credentials, skipping re-login")
            return False
        try:
            temp_page = await oe_client._context.new_page()
            try:
                await oe_client._login(temp_page)
                self._log.info("OrbitExch re-login succeeded")
                return True
            finally:
                await temp_page.close()
        except Exception as e:
            self._log.warning(f"OrbitExch re-login failed: {e}")
            return False

    def _sync_oe_executor_pages(self, oe_client) -> None:
        """把 odds_client 新创建的 competition page 同步给 executor（首次打开后必需）。"""
        if not self._execution_service:
            return
        for page_key, page in list(oe_client._pages.items()):
            if page_key == "main":
                continue
            self._execution_service.set_orbitexch_page(page_key, page)

    def _compute_rebates_safe(self) -> bool:
        """从已 fetch 的数据计算 way_rebate（不触发 IO）。"""
        if not self._odds_service:
            self._log.debug("Odds service not available, skipping rebate")
            return False
        try:
            self._refresh_all_way_rebates()
            return True
        except Exception as e:
            self._log.warning(f"Way rebate refresh failed: {e}")
            return False

    def get_health_status(self) -> dict:
        """获取健康检查状态"""
        next_at = self._next_health_check_at
        seconds_until_next = (
            max(0.0, next_at - time.monotonic()) if next_at is not None else None
        )
        return {
            "health_ok": self._health_ok,
            "polymarket": self._pm_healthy,
            "orbitexch": self._oe_healthy,
            "way_rebate": self._rebate_healthy,
            "blocked": self._health_check_blocked,
            "seconds_until_next_check": seconds_until_next,
        }

    async def initialize_cleanup(
        self,
        execution_config: ExecutionConfig,
        odds_config: OddsSubscriptionConfig,
    ) -> bool:
        """初始化 cleanup 组件（merge & claim）"""
        if not execution_config.cleanup_enabled:
            self._log.info("Cleanup disabled")
            return False

        contract_service = PolymarketContractService(
            config=odds_config,
            logger=logging.getLogger("PolymarketContractService"),
        )
        contract_ok = await contract_service.initialize()
        if not contract_ok:
            self._log.warning("PolymarketContractService init failed, cleanup disabled")
            return False

        self._cleanup = PostSessionCleanup(
            config=execution_config,
            contract_service=contract_service,
            logger=logging.getLogger("PostSessionCleanup"),
        )
        self._log.info("Cleanup initialized by risk service")
        return True

    def _on_session_complete_message(self, msg: Any) -> None:
        """
        处理会话完成消息

        调度异步任务：cleanup → 全量刷新 → 更新所有 way_rebate
        """
        if not isinstance(msg, SessionCompleteMessage):
            return

        pair_id = msg.pair_id

        # 调度异步任务：cleanup → 全量刷新 → 更新所有 way_rebate
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._post_session_refresh(pair_id))
        except RuntimeError:
            self._log.warning(f"No running event loop for post-session refresh of {pair_id}")

    async def _post_session_refresh(self, trigger_pair_id: str) -> None:
        """
        Session 结束后的完整刷新流程：
        1. 执行 cleanup（merge & claim）
        2. 全量刷新 odds_subscription 内存中的持仓和活跃订单
        3. 从 odds_subscription 读取全量数据，更新所有 pair 的 way_rebate

        使用 try-finally 确保无论成功失败都解锁 pair。
        """
        try:
            # 锁定 pair
            self._publish_pair_activity(trigger_pair_id, True, "risk")

            # 1. Cleanup (merge & claim)
            if self._cleanup and self._odds_service:
                try:
                    polymarket_client = self._odds_service.get_polymarket_client()
                    if polymarket_client:
                        cleanup_result = await self._cleanup.execute_global(polymarket_client)
                        self._log.info(f"Post-session cleanup: {cleanup_result.summary}")
                except Exception as e:
                    self._log.warning(f"Post-session cleanup error: {e}")

            # 2. 等待 Data API 索引更新后，全量刷新持仓和活跃订单
            await asyncio.sleep(5)
            if self._odds_service:
                try:
                    await self._odds_service.refresh_all_positions_and_orders()
                except Exception as e:
                    self._log.warning(f"Full refresh error: {e}")

            # 3. 从 odds_subscription 读取全量数据，更新所有 pair 的 way_rebate
            self._refresh_all_way_rebates()

        finally:
            # 确保解锁 pair（无论成功失败）
            self._publish_pair_activity(trigger_pair_id, False, "risk")
            self._log.info(f"Post-session refresh completed for {trigger_pair_id}, pair unlocked")

    def _refresh_all_way_rebates(self) -> None:
        """
        全量更新所有已订阅 pair 的持仓和 way_rebate

        使用新的 PositionManager 构建完整数据后原子替换，
        避免 clear() 造成的空窗期影响并发的 check_risk 调用。

        Polymarket merge 保护：
        - 如果某方向的 Polymarket 仓位变小（merge 导致），保留旧的较大仓位
        - 如果某方向的 Polymarket 仓位完全消失，检查 OrbitExch 是否还有该 pair 的仓位：
          - 有 → 比赛未结束，保留旧 Polymarket 仓位
          - 无 → 比赛已结束，不保留
        """
        if not self._odds_service:
            return

        # 快照旧 manager 中每个 pair 的 Polymarket 腿: {pair_id: {market_type: PositionLeg}}
        old_pm_legs: dict[str, dict[str, Any]] = {}
        for position in self._position_manager.get_all_positions():
            for leg in position.legs:
                if leg.venue == "polymarket":
                    old_pm_legs.setdefault(position.pair_id, {})[leg.market_type] = leg

        mappings = self._odds_service.get_position_mappings()
        all_polymarket_positions = self._odds_service.get_polymarket_positions()
        all_orbitexch_bets = self._odds_service.get_orbitexch_bets()
        self._log.info(
            "Refreshing all way rebates: "
            f"pm_positions={len(all_polymarket_positions)}, "
            f"oe_bets={len(all_orbitexch_bets)}, "
            f"pm_pairs={len(mappings.get('polymarket_pair_mapping', {}))}, "
            f"oe_pairs={len(mappings.get('orbitexch_pair_mapping', {}))}, "
            f"selection_pairs={len(mappings.get('selection_mappings', {}))}, "
            f"share={self._position_manager._default_share}, fx={self._fx}"
        )

        # 在新的 PositionManager 中构建全量持仓
        new_manager = PositionManager(default_share=self._position_manager._default_share)
        new_manager.set_fx(self._fx)
        new_manager.load_polymarket_positions(
            positions=all_polymarket_positions,
            pair_mapping=mappings.get("polymarket_pair_mapping", {}),
        )
        new_manager.load_orbitexch_bets(
            bets=all_orbitexch_bets,
            pair_mapping=mappings.get("orbitexch_pair_mapping", {}),
            selection_mappings=mappings.get("selection_mappings", {}),
        )

        # Polymarket merge 保护：保留因 merge 而缩小/消失的仓位
        for pair_id, old_legs_by_type in old_pm_legs.items():
            new_position = new_manager.get_position(pair_id)

            # 检查新 manager 中该 pair 是否还有任何仓位（PM 或 OrbitExch）
            pair_still_active = bool(new_position and new_position.legs)

            for market_type, old_leg in old_legs_by_type.items():
                # 找新 manager 中对应的 Polymarket 腿
                new_pm_leg = None
                if new_position:
                    for leg in new_position.legs:
                        if leg.venue == "polymarket" and leg.market_type == market_type:
                            new_pm_leg = leg
                            break

                if new_pm_leg is not None:
                    # 仓位变小 → 保留旧值（merge 只会减小不会增大）
                    if new_pm_leg.size < old_leg.size - 0.001:
                        self._log.info(
                            f"PM merge protection: {pair_id}/{market_type} "
                            f"size {new_pm_leg.size:.4f} → {old_leg.size:.4f} (kept old)"
                        )
                        new_pm_leg.size = old_leg.size
                        new_pm_leg.price = old_leg.price
                        new_pm_leg.profit_override = old_leg.profit_override
                        new_pm_leg.loss_override = old_leg.loss_override
                else:
                    # 仓位消失
                    if pair_still_active:
                        # 该 pair 仍有其他仓位 → 比赛未结束，恢复旧 Polymarket 腿
                        position = new_manager.get_or_create_position(pair_id)
                        position.add_leg(old_leg)
                        self._log.info(
                            f"PM merge protection: {pair_id}/{market_type} "
                            f"disappeared but pair still active, restored "
                            f"size={old_leg.size:.4f} price={old_leg.price:.4f}"
                        )
                    else:
                        self._log.info(
                            f"PM position cleared: {pair_id}/{market_type} "
                            f"disappeared and no other legs (match ended)"
                        )

        # 原子替换
        self._position_manager = new_manager

        all_positions = self._position_manager.get_all_positions()
        for position in all_positions:
            wr = self._position_manager.get_way_rebate(position.pair_id)
            self._log.info(
                f"way_rebate for {position.pair_id}: {wr}, "
                f"share={position.share}, legs=[{', '.join(f'{l.venue}/{l.market_type}/size={l.size}/price={l.price}/profit_override={l.profit_override}/loss_override={l.loss_override}/fx={l.fx}' for l in position.legs)}]"
            )
        self._log.info(
            f"All way_rebates refreshed: {len(all_positions)} positions"
        )

    def _publish_pair_activity(self, pair_id: str, is_active: bool, source: str) -> None:
        """发布 pair 活跃状态消息"""
        if not self._msgbus:
            return
        msg = PairActivityMessage(
            pair_id=pair_id,
            is_active=is_active,
            source=source,
        )
        topic = pair_activity_topic(pair_id)
        self._msgbus.publish(topic, msg)

    # =========================================================================
    # 历史持仓加载
    # =========================================================================

    def load_historical_positions(
        self,
        polymarket_positions: list,
        orbitexch_bets: list[dict],
        polymarket_pair_mapping: dict[str, str],
        orbitexch_pair_mapping: dict[str, str],
        selection_mappings: dict[str, dict[int, str]],
    ) -> dict[str, int]:
        """
        加载历史持仓数据

        在服务启动时调用，从 API 获取的历史持仓数据加载到 PositionManager。

        Args:
            polymarket_positions: PolymarketPosition 列表
            orbitexch_bets: OrbitExch bet 列表（dict 格式）
            polymarket_pair_mapping: event_id -> pair_id 映射
            orbitexch_pair_mapping: market_id -> pair_id 映射
            selection_mappings: pair_id -> {selection_id: market_type} 映射

        Returns:
            {"polymarket": count, "orbitexch": count}
        """
        # 清空现有持仓（重新加载）
        self._position_manager.clear()

        # 加载 Polymarket 持仓
        pm_count = self._position_manager.load_polymarket_positions(
            positions=polymarket_positions,
            pair_mapping=polymarket_pair_mapping,
        )

        # 加载 OrbitExch 持仓
        oe_count = self._position_manager.load_orbitexch_bets(
            bets=orbitexch_bets,
            pair_mapping=orbitexch_pair_mapping,
            selection_mappings=selection_mappings,
        )

        self._log.info(
            f"Loaded historical positions: Polymarket={pm_count}, OrbitExch={oe_count}"
        )

        # 记录各比赛的 way_rebate
        for position in self._position_manager.get_all_positions():
            way_rebate = position.calculate_way_rebate()
            if way_rebate:
                self._log.debug(
                    f"way_rebate for {position.pair_id}: {way_rebate}, "
                    f"share={position.share}, legs=[{', '.join(f'{l.venue}/{l.market_type}/size={l.size}/price={l.price}' for l in position.legs)}]"
                )

        return {"polymarket": pm_count, "orbitexch": oe_count}

    # =========================================================================
    # 持仓管理
    # =========================================================================

    def refresh_pair_position(
        self,
        pair_id: str,
        polymarket_positions: list,
        orbitexch_bets: list[dict],
        mappings: dict,
    ) -> None:
        """
        从 API 数据刷新指定 pair 的持仓

        在执行会话完成后调用，用 API 返回的实际持仓数据替换该 pair 的持仓 legs。
        数据比逐笔 add_fill 更准确，且与执行服务松耦合。

        Args:
            pair_id: 比赛 ID
            polymarket_positions: 该 pair 的 Polymarket 持仓列表
            orbitexch_bets: 该 pair 的 OrbitExch bet 列表
            mappings: 持仓映射 {polymarket_pair_mapping, orbitexch_pair_mapping, selection_mappings}
        """
        # 清除该 pair 的现有 legs（保留元信息）
        self._position_manager.refresh_position(pair_id)

        # 用 API 数据重建 legs
        pm_count = self._position_manager.load_polymarket_positions(
            positions=polymarket_positions,
            pair_mapping=mappings.get("polymarket_pair_mapping", {}),
        )

        oe_count = self._position_manager.load_orbitexch_bets(
            bets=orbitexch_bets,
            pair_mapping=mappings.get("orbitexch_pair_mapping", {}),
            selection_mappings=mappings.get("selection_mappings", {}),
        )

        # 输出刷新后的 way_rebate
        way_rebate = self._position_manager.get_way_rebate(pair_id)
        min_rebate = min(way_rebate.values()) if way_rebate else None

        self._log.info(
            f"Position refreshed: {pair_id}, "
            f"loaded Polymarket={pm_count}, OrbitExch={oe_count}, "
            f"way_rebate={way_rebate}, min={min_rebate}"
        )

    def close_match(self, pair_id: str) -> None:
        """
        标记比赛已结束

        已结束的比赛不参与全局止损计算。

        Args:
            pair_id: 比赛 ID
        """
        self._position_manager.close_match(pair_id)
        self._log.info(f"Match closed: {pair_id}")

    # =========================================================================
    # 风控检查
    # =========================================================================

    def check_risk(self, pair_id: str) -> RiskCheckResult:
        """
        检查是否允许下注

        检查顺序：
        1. 风控是否启用
        2. 单场止盈检查（所有方向返水率 >= tp）
        3. 单场止损检查（最小方向返水率 < sl）
        4. 全局累计止损检查

        Args:
            pair_id: 比赛 ID

        Returns:
            检查结果
        """
        # 执行开关关闭 — 阻止所有执行
        if not self._config.execution_enabled:
            return RiskCheckResult(allowed=False, reason="Execution disabled")

        # 健康检查未通过 — 拒绝机会
        if not self._health_ok:
            return RiskCheckResult(
                allowed=False,
                reason=f"Health check failed (pm={'OK' if self._pm_healthy else 'FAIL'}, "
                       f"oe={'OK' if self._oe_healthy else 'FAIL'}, "
                       f"rebate={'OK' if self._rebate_healthy else 'FAIL'})",
            )

        # 风控未启用
        if not self._config.enabled:
            return RiskCheckResult(allowed=True, reason="Risk disabled")

        # 赔率缺失或异常检查
        odds_check = self._check_odds_valid(pair_id)
        if odds_check is not None:
            return odds_check

        # 获取持仓数据
        position = self._position_manager.get_position(pair_id)
        way_rebate = position.calculate_way_rebate() if position else {}
        min_way_rebate = min(way_rebate.values()) if way_rebate else None
        global_min_sum = self._position_manager.get_global_min_rebate_sum()
        if position:
            self._log.debug(
                f"Risk check input: pair_id={pair_id}, share={position.share}, "
                f"way_rebate={way_rebate}, min_way_rebate={min_way_rebate}, "
                f"legs=[{', '.join(f'{l.venue}/{l.market_type}/size={l.size}/price={l.price}/profit_override={l.profit_override}/loss_override={l.loss_override}/fx={l.fx}' for l in position.legs)}]"
            )
        else:
            self._log.debug(
                f"Risk check input: pair_id={pair_id}, no position, "
                f"way_rebate={way_rebate}, min_way_rebate={min_way_rebate}"
            )

        # 1. 单场止盈检查：所有方向返水率 >= tp
        if way_rebate:
            match_tp = self._config.match_tp
            all_above_tp = all(rebate >= match_tp for rebate in way_rebate.values())
            if all_above_tp:
                self._log.info(
                    f"Match take profit triggered: {pair_id}, "
                    f"all way_rebate >= tp={match_tp:.2%}, way_rebate={way_rebate}"
                )
                return RiskCheckResult(
                    allowed=False,
                    reason=f"Match take profit: all way_rebate >= {match_tp:.2%}",
                    tp_blocked=True,
                    way_rebate=way_rebate,
                    min_way_rebate=min_way_rebate,
                    global_min_sum=global_min_sum,
                )

        # 2. 单场止损检查
        if min_way_rebate is not None:
            match_sl = self._config.get_match_sl(pair_id)
            if min_way_rebate < match_sl:
                self._log.warning(
                    f"Match stop loss triggered: {pair_id}, "
                    f"min_way_rebate={min_way_rebate:.4f} < sl={match_sl}"
                )
                return RiskCheckResult(
                    allowed=False,
                    reason=f"Match stop loss: min_way_rebate={min_way_rebate:.2%} < {match_sl:.2%}",
                    match_blocked=True,
                    way_rebate=way_rebate,
                    min_way_rebate=min_way_rebate,
                    global_min_sum=global_min_sum,
                )

        # 3. 全局累计止损检查
        if global_min_sum < self._config.global_sl:
            self._log.warning(
                f"Global stop loss triggered: "
                f"global_min_sum={global_min_sum:.4f} < sl={self._config.global_sl}"
            )
            return RiskCheckResult(
                allowed=False,
                reason=f"Global stop loss: sum={global_min_sum:.2%} < {self._config.global_sl:.2%}",
                global_blocked=True,
                way_rebate=way_rebate,
                min_way_rebate=min_way_rebate,
                global_min_sum=global_min_sum,
            )

        # 通过检查
        return RiskCheckResult(
            allowed=True,
            way_rebate=way_rebate,
            min_way_rebate=min_way_rebate,
            global_min_sum=global_min_sum,
        )

    def check_balance(self, pair_id: str, share: float, direction) -> RiskCheckResult:
        """
        余额门控检查

        检查各平台余额是否足够本次下单（包含活跃订单）

        Args:
            pair_id: 比赛 ID
            share: adjusted_share（调整后的份额系数）
            direction: best_direction（套利方向，包含各腿信息）

        Returns:
            检查结果
        """
        # 计算各平台需要的实际 size（下单金额）
        pm_required_size = 0.0  # Polymarket 需要的金额（USDC）
        oe_required_size = 0.0  # OrbitExch 需要的金额（GBP）

        for leg in direction.legs:
            if leg.venue.value == "polymarket":
                # Polymarket: size = share
                pm_required_size += share
            else:  # orbitexch
                # OrbitExch: size = share / odds / fx
                if leg.raw_odds > 0:
                    oe_required_size += share / leg.raw_odds / self._fx

        # 检查 Polymarket 余额
        if pm_required_size > 0:
            pm_active_orders = self._get_active_polymarket_orders_total()
            pm_total_required = pm_required_size + pm_active_orders

            if self._pm_balance < pm_total_required:
                return RiskCheckResult(
                    allowed=False,
                    reason=(
                        f"PM balance insufficient: {self._pm_balance:.2f} < {pm_total_required:.2f} "
                        f"(this order: {pm_required_size:.2f}, active: {pm_active_orders:.2f})"
                    ),
                )

        # 检查 OrbitExch 余额
        if oe_required_size > 0:
            oe_active_orders = self._get_active_orbitexch_orders_total()
            oe_total_required = oe_required_size + oe_active_orders

            if self._oe_balance < oe_total_required:
                return RiskCheckResult(
                    allowed=False,
                    reason=(
                        f"OE balance insufficient: {self._oe_balance:.2f} < {oe_total_required:.2f} "
                        f"(this order: {oe_required_size:.2f}, active: {oe_active_orders:.2f})"
                    ),
                )

        # 余额充足
        return RiskCheckResult(allowed=True)

    def _get_active_polymarket_orders_total(self) -> float:
        """获取 Polymarket 活跃订单总金额（USDC）"""
        if not self._odds_service:
            return 0.0

        try:
            active_orders = self._odds_service._polymarket_client.get_current_orders()
            # 活跃订单金额 = 原始金额 - 已成交金额
            total = sum(
                (order.original_size - order.size_matched)
                for order in active_orders
                if order.status == "LIVE"  # 只统计活跃订单
            )
            return total
        except Exception as e:
            self._log.warning(f"Failed to get Polymarket active orders: {e}")
            return 0.0

    def _get_active_orbitexch_orders_total(self) -> float:
        """获取 OrbitExch 活跃订单总金额（GBP）"""
        if not self._odds_service:
            return 0.0

        try:
            active_orders = self._odds_service._orbitexch_client.get_active_orders()
            # 统计未成交金额（sizeRemaining），不是下单金额（sizePlaced）
            total = sum(float(order.get("sizeRemaining", 0)) for order in active_orders)
            return total
        except Exception as e:
            self._log.warning(f"Failed to get OrbitExch active orders: {e}")
            return 0.0

    def _check_odds_valid(self, pair_id: str) -> RiskCheckResult | None:
        """
        检查赔率是否缺失或异常

        规则：
        - 任意平台任意方向赔率缺失 → 拒绝
        - 任意平台任意方向赔率 > 99 或 < 1.03 → 拒绝
        """
        if not self._odds_service:
            return RiskCheckResult(allowed=False, reason="Odds service not ready")

        odds = self._odds_service.get_latest_odds(pair_id)
        if not odds:
            return RiskCheckResult(allowed=False, reason="Odds missing")

        outcomes = {"home", "away"}
        if "draw" in odds.get("polymarket", {}) or "draw" in odds.get("orbitexch", {}):
            outcomes.add("draw")

        for venue in ("polymarket", "orbitexch"):
            venue_odds = odds.get(venue, {})
            for outcome in outcomes:
                market_data = venue_odds.get(outcome)
                if not market_data:
                    return RiskCheckResult(
                        allowed=False,
                        reason=f"Odds missing: {venue}/{outcome}",
                    )
                if venue == "polymarket":
                    bid = market_data.get("bid", 0)
                    ask = market_data.get("ask", 0)
                    if bid <= 0 or ask <= 0:
                        return RiskCheckResult(
                            allowed=False,
                            reason=f"Odds missing: {venue}/{outcome}",
                        )
                    for price in (bid, ask):
                        odds_value = 1 / price
                        if odds_value > 99 or odds_value < 1.03:
                            return RiskCheckResult(
                                allowed=False,
                                reason=f"Odds out of range: {venue}/{outcome}",
                            )
                else:
                    back = market_data.get("back", 0)
                    lay = market_data.get("lay", 0)
                    if back <= 0 or lay <= 0:
                        return RiskCheckResult(
                            allowed=False,
                            reason=f"Odds missing: {venue}/{outcome}",
                        )
                    for odds_value in (back, lay):
                        if odds_value > 99 or odds_value < 1.03:
                            return RiskCheckResult(
                                allowed=False,
                                reason=f"Odds out of range: {venue}/{outcome}",
                            )

        return None

    def is_match_allowed(self, pair_id: str) -> bool:
        """
        快速检查是否允许下注

        Args:
            pair_id: 比赛 ID

        Returns:
            是否允许
        """
        return self.check_risk(pair_id).allowed

    # =========================================================================
    # 查询
    # =========================================================================

    def get_position(self, pair_id: str) -> MatchPosition | None:
        """获取比赛持仓"""
        return self._position_manager.get_position(pair_id)

    def get_way_rebate(self, pair_id: str) -> dict[str, float]:
        """获取比赛各方向返水率"""
        return self._position_manager.get_way_rebate(pair_id)

    def _has_open_orders(self) -> bool:
        """是否存在未完全成交订单"""
        if not self._odds_service:
            return False

        # Polymarket 未完全成交订单
        for order in self._odds_service.get_polymarket_open_orders():
            size_remaining = float(getattr(order, "original_size", 0)) - float(
                getattr(order, "size_matched", 0)
            )
            if size_remaining > 0:
                return True

        # OrbitExch 未完全成交订单
        for bet in self._odds_service.get_orbitexch_bets():
            if float(bet.get("sizeRemaining", 0)) > 0:
                return True

        return False

    def get_way_rebate_by_venue(self, pair_id: str) -> dict[str, dict[str, float]]:
        """获取比赛按平台拆分的返水率"""
        position = self._position_manager.get_position(pair_id)
        if not position:
            return {}
        return position.calculate_way_rebate_by_venue()

    def get_all_way_rebates(self) -> dict[str, dict[str, float]]:
        """获取所有比赛的返水率"""
        result = {}
        for position in self._position_manager.get_all_positions():
            result[position.pair_id] = position.calculate_way_rebate()
        return result

    def get_global_status(self) -> dict[str, Any]:
        """
        获取全局风控状态

        Returns:
            {
                "enabled": bool,
                "match_sl": float,
                "global_sl": float,
                "match_tp": float,
                "global_min_sum": float,
                "global_blocked": bool,
                "active_positions": int,
                "blocked_matches": list[str],
                "tp_matches": list[str],
            }
        """
        global_min_sum = self._position_manager.get_global_min_rebate_sum()
        global_blocked = global_min_sum < self._config.global_sl if self._config.enabled else False

        # 检查每场比赛是否被阻止（止损或止盈）
        blocked_matches = []
        tp_matches = []
        for position in self._position_manager.get_active_positions():
            way_rebate = position.calculate_way_rebate()
            min_rebate = position.get_min_way_rebate()

            # 检查止盈：所有方向返水率 >= tp
            if way_rebate:
                all_above_tp = all(rebate >= self._config.match_tp for rebate in way_rebate.values())
                if all_above_tp:
                    tp_matches.append(position.pair_id)
                    continue  # 止盈的不再检查止损

            # 检查止损
            if min_rebate is not None:
                match_sl = self._config.get_match_sl(position.pair_id)
                if min_rebate < match_sl:
                    blocked_matches.append(position.pair_id)

        return {
            "enabled": self._config.enabled,
            "match_sl": self._config.match_sl,
            "global_sl": self._config.global_sl,
            "match_tp": self._config.match_tp,
            "global_min_sum": global_min_sum,
            "global_blocked": global_blocked,
            "active_positions": len(self._position_manager.get_active_positions()),
            "total_positions": len(self._position_manager.get_all_positions()),
            "blocked_matches": blocked_matches,
            "tp_matches": tp_matches,
        }

    def get_positions_summary(self) -> dict[str, Any]:
        """获取持仓摘要"""
        return self._position_manager.to_dict()

    def clear_positions(self) -> None:
        """清空所有持仓（谨慎使用）"""
        self._position_manager.clear()
        self._log.warning("All positions cleared")
