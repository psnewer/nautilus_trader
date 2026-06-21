"""Risk 层运行参数(share / fx / 单场 profit gates)。

NT 的 `Portfolio.__init__` / `LiveRiskEngine.__init__` 实参表固定(无自定义槽位),
故这些领域参数在 kernel 原生构造子类后,由 launcher 经 setter 注入(见 bootstrap.py)。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArbRiskParams:
    """单场止盈/止损门限 + 组合指标运行参数。"""

    # share 是 Risk profit gates 的目标规模基数:profit threshold = share * match_{tp,sl}。
    share: float = 100.0
    max_leg_share: float | None = None  # 单腿最大 share;None 表示不启用 adjusted size gate
    fx: float = 1.0  # OrbitExch 货币换算汇率(USD↔下注币种)

    # 单场硬停(逐 submit deny = "别开新仓")
    match_tp: float = 0.05      # 所有 outcome net_profit > share*tp → deny(已赚够别加)
    match_sl: float = -0.05     # 所有 outcome net_profit < share*sl → deny(该场恶化别加)
    global_sl: float = -0.10    # 旧配置兼容字段;Risk 不再执行全局止盈/止损门控
