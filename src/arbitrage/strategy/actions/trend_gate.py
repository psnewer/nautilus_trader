"""TrendGateAction —— 按 pair 级跨 venue/outcome 一致的价格趋势筛 leg(#329,消费 #328)。"""

from __future__ import annotations

import logging
import math

from src.arbitrage.strategy.checks.quote_legs import instrument_info
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)
_UP = {"up", "bigger", "larger", "rise", "rising"}
_DOWN = {"down", "smaller", "fall", "falling", "lower"}
_OUTCOMES = ("yes", "no")


class TrendGateAction(Action):
    """只保留概率朝设定方向变动的 outcome 的 leg;趋势要求**跨 venue、跨 outcome 一致**。

    **一致性判据(用户定 2026-08-09)**:一个"干净上升趋势"的 outcome O =
    O 的**所有 venue 腿都 Δprob ≥ 0(up 或不变)**,且互斥 outcome 的**所有 venue 腿都 Δprob ≤ 0
    (down 或不变)**,且至少一处严格移动(否则全平=无趋势)。complement 对称成立时它就是下降 outcome。
    任一 venue 反向(如某 venue 的 yes 跌而别处 yes 涨)→ 不是干净趋势 → 本轮不过滤。
    `steps` 不存在时保持该判据;存在时还要求全部参与判定的 leg 满足 `Σ|Δprob| >= steps`。

    `trend="up"`(**默认,概率变大**)保留干净上升 outcome 的腿;`trend="down"`(概率变小)保留其
    互斥(下降)outcome 的腿。趋势读 `ctx.price_trend`(§3.8.3,key=`str(instrument_id)` → Δprob,
    概率空间 PM/OE/SE 已同口径,不二次转)。一致性判据**扫该 pair 全部 tradable 腿**(含未执行的
    OE/SE),不只 candidate 内的腿。

    筛选语义:**符合要求的腿留下,不符合的删除**。符合 = 属于目标(期望方向)outcome 且该 outcome
    是干净一致趋势。因此:
    - 有干净趋势:留目标 outcome 的腿,删互斥 outcome 的腿;
    - **无干净一致趋势**(某 venue 反向 / 全平无严格移动 / `price_trend` 空 / 缺 pair_registry)
      → **没有任何腿符合 → 全删**(candidate 清空,本轮不下该单);
    - outcome 无法解析的腿也不符合 → 删。

    边界:
    - 某腿**无趋势数据**(未接入 / 首帧无 prev)→ 当作 **不变(flat)** 参与一致性(不因数据尚未攒齐
      就否定趋势;但若因此无任何严格移动 → 全平 → 无趋势 → 全删)。
    - 只处理 `candi_select` 选出的 `selected_candidate`,回写 `selected_candidate["legs"]` 与
      `scratch["legs"]`、元数据不变;撤单 candidate / 空 legs 不处理。
    - `trend` 非法值构造即 `ValueError`(fail-fast)。
    - `up` 缺失时沿用 `trend`；显式 `True/False` 分别保留上升/下降 outcome，并覆盖 `trend`。
    - `steps` 若配置,必须是有限非负数;等于累计 momentum 阈值时通过。
    """

    def __init__(
        self,
        trend: str = "up",
        steps: float | None = None,
        up: bool | None = None,
    ) -> None:
        if up is not None and not isinstance(up, bool):
            raise ValueError(f"trend_gate: up must be a boolean, got {up!r}")
        direction = str(trend).strip().lower()
        if up is not None:
            self._keep_rising = up
        elif direction in _DOWN:
            self._keep_rising = False
        elif direction in _UP:
            self._keep_rising = True
        else:
            raise ValueError(f"trend_gate: invalid trend={trend!r}, expected up/down")
        self._steps = None if steps is None else float(steps)
        if self._steps is not None and (not math.isfinite(self._steps) or self._steps < 0):
            raise ValueError(f"trend_gate: steps must be a finite non-negative number, got {steps!r}")

    async def execute(self, ctx: EvalContext) -> None:
        selected = ctx.scratch.get("selected_candidate")
        if not isinstance(selected, dict) or selected.get("cancel_pair_orders"):
            return
        legs = selected.get("legs")
        if not isinstance(legs, list) or not legs:
            return

        # 干净一致趋势才有目标 outcome;否则 target=None → 无腿符合 → 全删。
        rising = None
        if ctx.pair_registry is not None:
            rising = _coherent_rising_outcome(ctx, ctx.price_trend or {}, steps=self._steps)
        target = None if rising is None else (rising if self._keep_rising else _complement(rising))

        kept = []
        for leg in legs:
            if target is not None and _outcome(leg) == target:
                kept.append(leg)  # 符合:目标 outcome 的腿
                continue
            _LOG.info(
                f"TrendGate: pair={ctx.pair_id} drop leg={leg.get('instrument_id')} "
                f"outcome={_outcome(leg)} rising={rising} keep_target={target}",
            )
        filtered = dict(selected)
        filtered["legs"] = kept
        ctx.scratch["selected_candidate"] = filtered
        ctx.scratch["legs"] = kept


def _coherent_rising_outcome(
    ctx: EvalContext,
    trend: dict,
    *,
    steps: float | None = None,
) -> str | None:
    """返回跨 venue/outcome 一致的"概率变大"outcome;无干净趋势 → None。

    扫该 pair 全部 tradable 腿,按 outcome 聚合各 venue 的 Δprob(缺数据当 flat=0)。
    上升 outcome O 判据:O 各 venue 腿 Δ≥0(up/flat)、互斥 outcome 各 venue 腿 Δ≤0(down/flat),
    且至少一处严格移动(O 涨或互斥跌均可)。`steps` 存在时另要求全部腿 `Σ|Δprob| >= steps`。
    """
    by_outcome: dict[str, list[float]] = {"yes": [], "no": []}
    for iid in ctx.pair_registry.instrument_ids_for_pair(ctx.pair_id):
        info = instrument_info(ctx, iid)
        outcome = str(info.get("claim") or info.get("selection_role") or "").strip().lower()
        if outcome not in _OUTCOMES:
            continue
        delta = trend.get(str(iid))
        try:
            by_outcome[outcome].append(0.0 if delta is None else float(delta))
        except (TypeError, ValueError):
            by_outcome[outcome].append(0.0)
    if not by_outcome["yes"] or not by_outcome["no"]:
        return None  # 缺任一 outcome 的腿 → 无从判互斥一致
    if steps is not None:
        total_momentum = sum(abs(delta) for values in by_outcome.values() for delta in values)
        if total_momentum < steps:
            return None
    for up_o, down_o in (("yes", "no"), ("no", "yes")):
        ups, downs = by_outcome[up_o], by_outcome[down_o]
        all_up_flat = all(d >= 0 for d in ups)
        all_down_flat = all(d <= 0 for d in downs)
        strict = any(d > 0 for d in ups) or any(d < 0 for d in downs)
        if all_up_flat and all_down_flat and strict:
            return up_o
    return None


def _complement(outcome: str) -> str:
    return "no" if outcome == "yes" else "yes"


def _outcome(leg: dict) -> str:
    return str(leg.get("claim") or leg.get("role") or "").strip().lower()
