"""
套利组件接线 —— 在构造 TradingNode **之前**替换 kernel 模块的类名,使 NT 原生构造我们的子类。

为什么用导入替换而非构造后 swap(决策见 refactor.md 修订记录):
- NT `system/kernel.py` 用模块级名字构造 `Portfolio(...)` / `LiveRiskEngine(...)` /
  `LiveExecutionEngine(...)`,无 class 注入点。
- 构造后 swap 需重连 msgbus 4 处 endpoint/订阅 + Trader 引用 + 在 node.start 前赋值,易碎。
- 替换 `nautilus_trader.system.kernel.Portfolio` / `.LiveRiskEngine` / `.LiveExecutionEngine`
  → kernel 原生构造子类,
  零摘除、零重注册。代价:依赖 kernel 模块结构(模块级 import 名),NT 升级时需复核。

实盘环境下 kernel 用的是 `LiveRiskEngine`(非基类 `RiskEngine`),故只替换 Live 版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

import nautilus_trader.live.node as _node
import nautilus_trader.system.kernel as _kernel

from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.common.params import ArbitrageParams
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.execution.engine import ArbLiveExecutionEngine
from src.arbitrage.risk.config import ArbRiskParams
from src.arbitrage.risk.engine import ArbitrageLiveRiskEngine
from src.arbitrage.risk.portfolio import ArbitragePortfolio


# ─────────────────────────────────────────────────────────────────────────
# 共享上下文 —— factory 注入通道
# ─────────────────────────────────────────────────────────────────────────
# NT 的 LiveExecClientFactory.create 签名固定 (loop, name, config, msgbus, cache, clock),
# 没法直传我们的额外依赖(venue_liveness / settlement / positions_fetcher / 间隔)。
# 用进程级共享上下文做注入通道:launcher 在 `node.build()` 之前 `prepare_arb_context(...)`
# 填好,自定义 factory 在 `create` 内 `get_arb_context()` 读出。同 install_arbitrage_engines 的
# import-替换思路:bootstrap 持共享态,NT 机制取用。


@dataclass
class ArbContext:
    """factory create 时读取的进程级共享件。"""

    venue_liveness: VenueExecutionLiveness | None = None  # enabled trading venues 共享同一份
    pair_registry: PairRegistry | None = None      # matching 唯一写;risk/portfolio/session 只读(#34)
    pair_inflight: object | None = None            # PairInFlightGate(§7);#261 起**仅 strategy 使用**(评估串行)

    # PM 专属
    pm_settlement: object | None = None
    # 注(#110):PM merge/redeem 改由 NT 连续 position 对账驱动(无 HealthCheckLoop)→
    # 不再需要 `pm_positions_fetcher` / `pm_health_interval_secs`(已删)。

    # Venue/DataSource Registry 第二阶段通用入口。PM/OE/SE factories 只读取这些 keyed map,
    # 避免继续扩散 venue 专属上下文形状。
    session_timeout_secs_by_venue: dict = field(default_factory=dict)
    discovery_config_by_venue: dict = field(default_factory=dict)
    sport_aliases_by_venue: dict = field(default_factory=dict)
    competition_aliases_by_venue: dict = field(default_factory=dict)
    instrument_provider_by_venue: dict = field(default_factory=dict)
    browser_manager_by_venue: dict = field(default_factory=dict)
    browser_lock_by_venue: dict = field(default_factory=dict)
    browser_login_state_by_venue: dict = field(default_factory=dict)
    target_competitions_by_data_source: dict = field(default_factory=dict)
    competition_to_sport_by_data_source: dict = field(default_factory=dict)

    # Debug 注入(Q11 / §6.6;`enabled=False` 或 None → 全套生产路径)
    debug_config: object | None = None  # `DebugConfig | None`;运行时类型,避免 bootstrap import debug 模块循环

    # Web Arbitrage 运行时默认值:share/max_leg_share/fx。OE adapter 用 fx 做边界换汇。
    arbitrage_params: ArbitrageParams | None = None

    # decimal venue(OE/SE)市价单开关(#256 续,`execution.market_order_enabled` 启动时定
    # 一份快照,重启生效;`PlaceBetsAction` 读取,详见 strategy 相关 architecture.md)。
    market_order_enabled: bool = False


_arb_context: ArbContext = ArbContext()


def ctx_map_get(ctx, attr: str, key: str, default=None):
    """读取 ArbContext keyed map;map 缺失或 key 缺失时返回显式默认值。"""
    mapping = getattr(ctx, attr, None) or {}
    return mapping.get(key, default)


def ctx_map_require(ctx, attr: str, key: str):
    """读取必需的 ArbContext keyed map 值;缺失时 fail-fast。"""
    mapping = getattr(ctx, attr, None) or {}
    if key not in mapping:
        raise RuntimeError(f"ArbContext.{attr}[{key!r}] is required")
    return mapping[key]


def ctx_map_set(ctx, attr: str, key: str, value) -> None:
    """写入 ArbContext keyed map;用于 factory 构造后回写共享对象。"""
    mapping = getattr(ctx, attr, None)
    if mapping is None:
        mapping = {}
        setattr(ctx, attr, mapping)
    mapping[key] = value


def ctx_map_get_or_create(ctx, attr: str, key: str, factory):
    """读取 ArbContext keyed map 值;缺失时用 factory 创建并回写。"""
    existing = ctx_map_get(ctx, attr, key)
    if existing is not None:
        return existing
    value = factory()
    ctx_map_set(ctx, attr, key, value)
    return value


def get_arb_context() -> ArbContext:
    return _arb_context


def prepare_arb_context(**kwargs) -> ArbContext:
    """构造 TradingNode **之后、`node.build()` 之前**调用一次,填好后 factory 才能取到。

    至少要传 `venue_liveness`(execution 写,risk 读;`wire_arbitrage_runtime` 用同一份)。
    """
    global _arb_context
    _arb_context = ArbContext(**kwargs)
    return _arb_context


def reset_arb_context() -> None:
    """测试 / 重启用。"""
    global _arb_context
    _arb_context = ArbContext()


class ArbNautilusKernel(_kernel.NautilusKernel):
    """#259:启动对账失败不再中止启动 —— 失败的 venue 由 `VenueExecutionLiveness` 精确隔离。

    上游 `kernel.py:1023-1025` 在 `_await_execution_reconciliation()` 返 False 时直接 `return`,
    于是 `self._trader.start()` 永不执行 → 全部 actor 卡 READY、web 不绑,且进程仍存活、除一行
    `Execution state could not be reconciled` 外无任何提示。而 PM data-api 启动期一次瞬时超时
    就足以触发(见 refactor.md #122/#259)。

    放行的安全性(逐条验证,勿凭直觉改动):
    - 失败时 venue 已被标 dead(`arb_execution.py` 的 `mark_*_dead` 在 `raise` 之前执行);
    - `risk/engine.py:_check_required_venues_alive` 对**任一**所需 venue 不 alive 即拒整个机会
      (读 `meta.expected_legs`,不会只下单边);`venue_alive` 要求 order+position 双 alive;
    - `execution_engine.py:1792-1794` 在 `finally` 里 set `_startup_reconciliation_event`,
      故连续对账循环照常启动 → 下一轮成功即 `mark_*_alive`,自动恢复。

    本类**只改「失败是否中止启动」**,对账逻辑与上游日志全部保留(仍调 `super()`)。
    """

    async def _await_execution_reconciliation(self) -> bool:
        ok = await super()._await_execution_reconciliation()
        if not ok:
            self._log.warning(
                "Execution reconciliation failed; continuing startup (#259). "
                "Affected venues are fail-closed via VenueExecutionLiveness — Risk denies any "
                "opportunity involving them — and will self-heal on the next successful "
                "continuous reconciliation cycle.",
            )
        return True


def install_arbitrage_engines(debug_config: object | None = None) -> None:
    """构造 TradingNode 之前调用一次。幂等。

    `debug_config`:`DebugConfig | None`;不传或 `enabled=False` → 装生产 `ArbitrageLiveRiskEngine`;
    `enabled=True` → 装一个 `DebugArbitrageLiveRiskEngine` 的薄包装(kernel 不会传 `debug=`,
    包装类在 __init__ 内从闭包注入)。Portfolio 不分 debug(本轮 Q11.A 只覆盖 Risk;
    `DebugArbitragePortfolio` 待后续 slice 按需做)。
    """
    _kernel.Portfolio = ArbitragePortfolio
    _kernel.LiveExecutionEngine = ArbLiveExecutionEngine
    # #259:`live/node.py:35` 直接 `from ... import NautilusKernel`,故替换 node 模块里的绑定
    # (替 `_kernel.NautilusKernel` 无效——node 已在 import 时取走名字)。
    _node.NautilusKernel = ArbNautilusKernel

    if debug_config is not None and getattr(debug_config, "enabled", False):
        from src.arbitrage.debug.risk import DebugArbitrageLiveRiskEngine

        class _KernelInjectedDebugEngine(DebugArbitrageLiveRiskEngine):
            """薄包装:kernel 按 LiveRiskEngine 实参表构造,不会传 `debug=`;闭包注入。"""

            def __init__(self, *args, **kwargs):
                super().__init__(*args, debug=debug_config, **kwargs)

        _kernel.LiveRiskEngine = _KernelInjectedDebugEngine
    else:
        _kernel.LiveRiskEngine = ArbitrageLiveRiskEngine


def wire_arbitrage_runtime(
    node,
    *,
    params: ArbRiskParams | None = None,
    arbitrage_params: ArbitrageParams | None = None,
    venue_liveness: VenueExecutionLiveness | None = None,
) -> VenueExecutionLiveness:
    """TradingNode 构造后、run 之前调用:把领域参数注入已原生构造的子类实例。

    返回共享的 VenueExecutionLiveness(execution/risk 接线时复用同一份)。
    """
    params = params or ArbRiskParams()
    arbitrage_params = arbitrage_params or ArbitrageParams()
    # 优先用 launcher 已经 prepare 进 ArbContext 的那份(execution factory / matching actor / runtime 共享同一对象)
    venue_liveness = venue_liveness or _arb_context.venue_liveness or VenueExecutionLiveness()
    pair_registry = _arb_context.pair_registry or PairRegistry()

    portfolio = node.kernel.portfolio
    if not isinstance(portfolio, ArbitragePortfolio):
        raise RuntimeError(
            "kernel.portfolio 不是 ArbitragePortfolio —— install_arbitrage_engines() "
            "必须在构造 TradingNode 之前调用",
        )
    portfolio.configure_arb(
        share=arbitrage_params.share,
        pair_registry=pair_registry,
    )

    risk_engine = node.kernel.risk_engine
    if not isinstance(risk_engine, ArbitrageLiveRiskEngine):
        raise RuntimeError(
            "kernel.risk_engine 不是 ArbitrageLiveRiskEngine —— install_arbitrage_engines() "
            "必须在构造 TradingNode 之前调用",
        )
    risk_engine.configure_arb(params, venue_liveness=venue_liveness, arbitrage_params=arbitrage_params)

    exec_engine = getattr(node.kernel, "exec_engine", None)
    if isinstance(exec_engine, ArbLiveExecutionEngine):
        exec_engine.configure_arb(pair_registry=pair_registry)   # #261:barrier 不再持有 pair 闸

    return venue_liveness
