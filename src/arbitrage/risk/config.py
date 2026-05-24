"""Risk 层运行参数(share / fx / 三门限)。

NT 的 `Portfolio.__init__` / `LiveRiskEngine.__init__` 实参表固定(无自定义槽位),
故这些领域参数在 kernel 原生构造子类后,由 launcher 经 setter 注入(见 bootstrap.py)。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArbRiskParams:
    """组合级硬停门限 + way_rebate 计算基准。"""

    # way_rebate 计算基准(平移自旧 PositionManager.default_share / fx)
    share: float = 100.0
    fx: float = 1.0  # OrbitExch 货币换算汇率(USD↔下注币种)

    # 三门限(平移自旧 check_risk;逐 submit deny = "别开新仓")
    match_tp: float = 0.05      # 该 pair 任一方向 rebate ≥ tp → deny(已赚够别加)
    match_sl: float = -0.05     # 该 pair min_way_rebate < sl → deny(该场恶化别加)
    global_sl: float = -0.10    # global_min_rebate_sum < global_sl → deny(账户级累计止损)
