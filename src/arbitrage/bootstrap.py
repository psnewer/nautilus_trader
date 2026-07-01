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

    venue_liveness: VenueExecutionLiveness | None = None  # 跨 PM/OE 共享同一份
    pair_registry: PairRegistry | None = None      # matching 唯一写;risk/portfolio/session 只读(#34)
    pair_inflight: object | None = None            # PairInFlightGate(§6.10 §7,per-pair 串行);strategy+execution 共享一份

    # PM 专属
    pm_settlement: object | None = None
    pm_session_timeout_secs: float = 30.0
    # 注(#110):PM merge/redeem 改由 NT 连续 position 对账驱动(无 HealthCheckLoop)→
    # 不再需要 `pm_positions_fetcher` / `pm_health_interval_secs`(已删)。

    # OE 专属
    oe_session_timeout_secs: float = 30.0
    # 注(#109):OE competition 页存活封装进 WS handler(无 HealthCheckLoop)→ 不再有 health interval。
    # OE Discovery:scraper config + Provider 写 info 时查 aliases(slice 7A / #46)
    oe_scraper_config: object | None = None  # OrbitExchVenueConfig | None;运行时类型避循环 import
    oe_sport_aliases: dict = field(default_factory=dict)
    oe_competition_aliases: dict = field(default_factory=dict)

    # Provider 实例回写(slice 8A / #47):data factory 构造完 provider 后写回此处。
    # #59:原读者 InstrumentRefresher 已退役(发现迁 DataClient),当前**无读者**(仅 prepare_arb_context
    # / 测试设值);保留字段备未来跨组件取 provider 用,删除需级联 prepare_arb_context 签名 + 测试。
    pm_instrument_provider: object | None = None
    oe_instrument_provider: object | None = None
    # OE 共享 BrowserManager(§6.2 单例):data factory 先建并回写,exec factory 复用同一实例
    # → 一个浏览器(data/exec 各取专属 page),而非各 new 一个(两窗口 bug,#62)。
    oe_browser_manager: object | None = None

    # SE 专属(第一阶段离线接入):按 OE 型 venue 复制 factory 注入形状,但 launcher 暂不注册。
    se_session_timeout_secs: float = 30.0
    se_discovery_config: object | None = None  # SharpExchVenueConfig | None;运行时类型避循环 import
    se_sport_aliases: dict = field(default_factory=dict)
    se_competition_aliases: dict = field(default_factory=dict)
    se_instrument_provider: object | None = None
    se_browser_manager: object | None = None
    se_browser_lock: object | None = None

    # PM 发现目标(#55):`ArbPolymarketInstrumentProvider.load_all_async` 读这两字段做 series-based 发现。
    pm_event_slug_tags: list = field(default_factory=list)        # 目标 competition 列表(如 ["atp"]);PM /sports `sport` 字段比对
    pm_competition_to_sport: dict = field(default_factory=dict)   # competition→sport map(如 {"atp": "Tennis"});provider 写 info["sport"]

    # Debug 注入(Q11 / §6.6;`enabled=False` 或 None → 全套生产路径)
    debug_config: object | None = None  # `DebugConfig | None`;运行时类型,避免 bootstrap import debug 模块循环

    # Web Arbitrage 运行时默认值:share/max_leg_share/fx。OE adapter 用 fx 做边界换汇。
    arbitrage_params: ArbitrageParams | None = None


_arb_context: ArbContext = ArbContext()


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


def install_arbitrage_engines(debug_config: object | None = None) -> None:
    """构造 TradingNode 之前调用一次。幂等。

    `debug_config`:`DebugConfig | None`;不传或 `enabled=False` → 装生产 `ArbitrageLiveRiskEngine`;
    `enabled=True` → 装一个 `DebugArbitrageLiveRiskEngine` 的薄包装(kernel 不会传 `debug=`,
    包装类在 __init__ 内从闭包注入)。Portfolio 不分 debug(本轮 Q11.A 只覆盖 Risk;
    `DebugArbitragePortfolio` 待后续 slice 按需做)。
    """
    _kernel.Portfolio = ArbitragePortfolio
    _kernel.LiveExecutionEngine = ArbLiveExecutionEngine

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
        exec_engine.configure_arb(
            pair_inflight=_arb_context.pair_inflight,
            pair_registry=pair_registry,
        )

    return venue_liveness
