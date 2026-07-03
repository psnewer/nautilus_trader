"""Risk 层运行参数(单场 profit gates / 概率门控)。

NT 的 `Portfolio.__init__` / `LiveRiskEngine.__init__` 实参表固定(无自定义槽位),
故这些领域参数在 kernel 原生构造子类后,由 launcher 经 setter 注入(见 bootstrap.py)。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArbRiskParams:
    """单场止盈/止损门限 + 概率门限。"""

    # 单场硬停(逐 submit deny = "别开新仓");金额基数来自 ArbitrageParams.share。
    match_tp: float = 0.05      # 所有 outcome net_profit > share*tp → deny(已赚够别加)
    match_sl: float = -0.05     # 所有 outcome net_profit < share*sl → deny(该场恶化别加)

    # 订单隐含概率闭区间:PM price 即概率;OE 十进制赔率换算为 1/price。
    min_probability: float = 0.03
    max_probability: float = 0.97
