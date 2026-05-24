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

    # PM 专属
    pm_settlement: object | None = None
    pm_positions_fetcher: Callable[[], Awaitable[list]] | None = None
    pm_session_timeout_secs: float = 30.0
    pm_health_interval_secs: float = 30.0

    # OE 专属
    oe_session_timeout_secs: float = 30.0
    oe_health_interval_secs: float = 30.0


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


def install_arbitrage_engines() -> None:
    """构造 TradingNode 之前调用一次。幂等。"""
    _kernel.Portfolio = ArbitragePortfolio
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
    # 优先用 launcher 已经 prepare 进 ArbContext 的那份(execution factory 与 runtime 共享同一对象)
    leg_settled = leg_settled or _arb_context.leg_settled or LegSettledRegistry()

    portfolio = node.kernel.portfolio
    if not isinstance(portfolio, ArbitragePortfolio):
        raise RuntimeError(
            "kernel.portfolio 不是 ArbitragePortfolio —— install_arbitrage_engines() "
            "必须在构造 TradingNode 之前调用",
        )
    portfolio.configure_arb(share=params.share, fx=params.fx, leg_settled=leg_settled)

    risk_engine = node.kernel.risk_engine
    if not isinstance(risk_engine, ArbitrageLiveRiskEngine):
        raise RuntimeError(
            "kernel.risk_engine 不是 ArbitrageLiveRiskEngine —— install_arbitrage_engines() "
            "必须在构造 TradingNode 之前调用",
        )
    risk_engine.configure_arb(params)

    return leg_settled
