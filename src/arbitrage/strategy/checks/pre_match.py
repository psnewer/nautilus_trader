"""
PreMatchCheck —— 赛前盘门控(slice 9 / #49)。

Passes True ⇔ pair 未开赛(`snapshot.in_play=False`)。

`snapshot.in_play` 由 `build_snapshot` 从 OE leg 的 `cache.instrument.info["in_play"]` 派生
(OE WS `marketDefinition.inPlay` → `OrbitExchDataClient._on_price_frame` 写入)。

**用法**(放在 checktion 列表前位,利用 Python `all` 短路):
```json
"checktion": [
  {"type": "pre_match"},
  {"type": "mean_rebate", "params": {"min_rate": 0.01}}
]
```
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
