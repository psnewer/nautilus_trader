"""
套利组件接线 —— 在构造 TradingNode **之前**替换 kernel 模块的类名,使 NT 原生构造我们的子类。

为什么用导入替换而非构造后 swap(决策见 refactor.md 修订记录):
- NT `system/kernel.py` 用模块级名字构造 `Portfolio(...)` / `LiveRiskEngine(...)`,无 class 注入点。
- 构造后 swap 需重连 msgbus 4 处 endpoint/订阅 + Trader 引用 + 在 node.start 前赋值,易碎。
- 替换 `nautilus_trader.system.kernel.Portfolio` / `.LiveRiskEngine` → kernel 原生构造子类,
  零摘除、零重注册。代价:依赖 kernel 模块结构(模块级 import 名),NT 升级时需复核。

实盘环境下 kernel 用的是 `LiveRiskEngine`(非基类 `RiskEngine`),故只替换 Live 版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Awaitable
from typing import Callable

import nautilus_trader.system.kernel as _kernel

from src.arbitrage.common.leg_settled import LegSettledRegistry
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.risk.config import ArbRiskParams
from src.arbitrage.risk.engine import ArbitrageLiveRiskEngine
from src.arbitrage.risk.portfolio import ArbitragePortfolio


# ─────────────────────────────────────────────────────────────────────────
# 共享上下文 —— factory 注入通道
# ─────────────────────────────────────────────────────────────────────────
# NT 的 LiveExecClientFactory.create 签名固定 (loop, name, config, msgbus, cache, clock),
# 没法直传我们的额外依赖(leg_settled / settlement / positions_fetcher / 间隔)。
# 用进程级共享上下文做注入通道:launcher 在 `node.build()` 之前 `prepare_arb_context(...)`
# 填好,自定义 factory 在 `create` 内 `get_arb_context()` 读出。同 install_arbitrage_engines 的
# import-替换思路:bootstrap 持共享态,NT 机制取用。


@dataclass
class ArbContext:
    """factory create 时读取的进程级共享件。"""

    leg_settled: LegSettledRegistry | None = None  # 跨 PM/OE 共享同一份
    pair_registry: PairRegistry | None = None      # matching 唯一写;risk/portfolio/session 只读(#34)

    # PM 专属
    pm_settlement: object | None = None
    pm_positions_fetcher: Callable[[], Awaitable[list]] | None = None
    pm_session_timeout_secs: float = 30.0
    pm_health_interval_secs: float = 30.0

    # OE 专属
    oe_session_timeout_secs: float = 30.0
    oe_health_interval_secs: float = 30.0
    # OE Discovery:scraper config + Provider 写 info 时查 aliases(slice 7A / #46)
    oe_scraper_config: object | None = None  # OrbitExchVenueConfig | None;运行时类型避循环 import
    oe_sport_aliases: dict = field(default_factory=dict)
    oe_competition_aliases: dict = field(default_factory=dict)

    # Provider 实例回写(slice 8A / #47):data factory 构造完 provider 后写回此处。
    # #59:原读者 InstrumentRefresher 已退役(发现迁 DataClient),当前**无读者**(仅 prepare_arb_context
    # / 测试设值);保留字段备未来跨组件取 provider 用,删除需级联 prepare_arb_context 签名 + 测试。
    pm_instrument_provider: object | None = None
    oe_instrument_provider: object | None = None

    # PM 发现目标(#55):`ArbPolymarketInstrumentProvider.load_all_async` 读这两字段做 series-based 发现。
    pm_event_slug_tags: list = field(default_factory=list)        # 目标 competition 列表(如 ["atp"]);PM /sports `sport` 字段比对
    pm_competition_to_sport: dict = field(default_factory=dict)   # competition→sport map(如 {"atp": "Tennis"});provider 写 info["sport"]

    # Debug 注入(Q11 / §6.6;`enabled=False` 或 None → 全套生产路径)
    debug_config: object | None = None  # `DebugConfig | None`;运行时类型,避免 bootstrap import debug 模块循环


_arb_context: ArbContext = ArbContext()


def get_arb_context() -> ArbContext:
    return _arb_context


def prepare_arb_context(**kwargs) -> ArbContext:
    """构造 TradingNode **之后、`node.build()` 之前**调用一次,填好后 factory 才能取到。

    至少要传 `leg_settled`(execution 写、risk/portfolio 读;`wire_arbitrage_runtime` 用同一份)。
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
    leg_settled: LegSettledRegistry | None = None,
) -> LegSettledRegistry:
    """TradingNode 构造后、run 之前调用:把领域参数注入已原生构造的子类实例。

    返回共享的 LegSettledRegistry(execution 接线时复用同一份)。
    """
    params = params or ArbRiskParams()
    # 优先用 launcher 已经 prepare 进 ArbContext 的那份(execution factory / matching actor / runtime 共享同一对象)
    leg_settled = leg_settled or _arb_context.leg_settled or LegSettledRegistry()
    pair_registry = _arb_context.pair_registry or PairRegistry()

    portfolio = node.kernel.portfolio
    if not isinstance(portfolio, ArbitragePortfolio):
        raise RuntimeError(
            "kernel.portfolio 不是 ArbitragePortfolio —— install_arbitrage_engines() "
            "必须在构造 TradingNode 之前调用",
        )
    portfolio.configure_arb(
        share=params.share, fx=params.fx,
        leg_settled=leg_settled, pair_registry=pair_registry,
    )

    risk_engine = node.kernel.risk_engine
    if not isinstance(risk_engine, ArbitrageLiveRiskEngine):
        raise RuntimeError(
            "kernel.risk_engine 不是 ArbitrageLiveRiskEngine —— install_arbitrage_engines() "
            "必须在构造 TradingNode 之前调用",
        )
    risk_engine.configure_arb(params)

    return leg_settled
