"""
PreMatchCheck —— 赛前盘门控(slice 9 / #49)。

Passes True ⇔ pair 未开赛(`snapshot.in_play=False`)。

`snapshot.in_play` 由 `build_snapshot` 从 OE leg 的 `cache.instrument.info["in_play"]` 派生
(OE WS `marketDefinition.inPlay` → `OrbitExchDataClient._on_price_frame` 写入)。

**推荐用法**:生产配置用 `StrategyEvaluator` 派生的 `pre_match` signal 做 condition 级门控:
```json
"self_hits": {"signal": "pre_match"}
```

本 Check 类保留给兼容配置 / 单元测试,需要时仍可放在 `checktion` 前位。
"""

from __future__ import annotations

from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


class PreMatchCheck(Check):
    """无参 Check。仅看 `snapshot.in_play`。"""

    def passes(self, ctx: EvalContext) -> bool:
        snap = ctx.snapshot
        if snap is None:
            return True  # 无 snapshot 时不卡(让其他 Check 决定)
        return not getattr(snap, "in_play", False)
