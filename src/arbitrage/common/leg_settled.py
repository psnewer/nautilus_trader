"""
LegSettledRegistry —— 跨组件共享的 leg_settled 状态(execution 写,risk/portfolio/strategy 读)。

语义真理源见 `docs/arbitrage/architectures/execution/architecture.md §4.4`:
- leg_settled = "execution 启动后通讯通道存活信号"(非"已完全成交")。
- `true` = 至少一次 venue 确认事件已落库;`false` = execution 启动但从未收到该腿任何事件。
- **腿键 = `instrument_id`**(一个 instrument = 一条腿,全局唯一,不需 方向→下标 映射):
  `dict[pair_id, dict[instrument_id, bool]]`。首次 execution 创建,不删;每次新 execution
  用本轮提交的腿 instrument_id 集合整组重置 false;非 execution 触发事件不创建 entry,
  但命中已有 entry 时也置 true。

本对象是 P11 横切机制的"共享契约对象":单进程单 asyncio loop、纯内存、无序列化。
由 launcher 构造一份,注入 execution(写)与 risk/portfolio/strategy(读)。
"""

from __future__ import annotations

from typing import Hashable


class LegSettledRegistry:
    """进程内共享的 leg_settled 状态表。线程不安全,仅供单 asyncio loop 串行访问。

    `pair_id` 为比赛级聚合键(由 matching 算出,经 `PairRegistry` 暴露;#34 起不再用 info["competition"]);腿键为该腿的 `instrument_id`。
    """

    def __init__(self) -> None:
        self._settled: dict[str, dict[Hashable, bool]] = {}

    # ── execution 写入侧 ──────────────────────────────────────────────
    def arm(self, pair_id: str, instrument_id: Hashable) -> None:
        """某腿 submit+track 启动:把该腿置 false(创建 entry / 合并,**不动同 pair 其它腿**)。

        per-leg 语义(refactor.md #25):对套利(每场腿集合稳定)等价于"整组重置",
        对单腿补发更精确。"""
        self._settled.setdefault(pair_id, {})[instrument_id] = False

    def reset(self, pair_id: str, instrument_ids) -> None:
        """整组重置:用给定腿 instrument_id 集合替换该 pair 的全部腿为 false(整轮提交场景)。"""
        self._settled[pair_id] = {iid: False for iid in instrument_ids}

    def mark(self, pair_id: str, instrument_id: Hashable) -> None:
        """命中某腿的 venue 确认事件 → 置 true。entry 或该腿不存在则忽略(非 execution 触发不创建)。"""
        legs = self._settled.get(pair_id)
        if legs is not None and instrument_id in legs:
            legs[instrument_id] = True

    def mark_venue(self, venue_value: str) -> int:
        """#105:某 venue 一次完整 order 真值快照 / 成功 reconcile → **该 venue 所有 armed 腿置 true**。
        腿键 `instrument_id` 的 `.venue.value == venue_value` 即命中(缺席快照的腿 = 已澄清"没成功",亦置 true)。
        返回新置位腿数。与 PM "拉取成功该 venue 所有方向置 true"(§4.4)同逻辑;**只挂 order 真值,不碰 position**
        (execution `architecture.md §4.3bis(5)`)。注:腿键非该 venue(无 `.venue.value`)或已 true 的跳过。"""
        n = 0
        for legs in self._settled.values():
            for iid in list(legs):
                if legs[iid]:
                    continue
                if getattr(getattr(iid, "venue", None), "value", None) == venue_value:
                    legs[iid] = True
                    n += 1
        return n

    # ── risk / portfolio / strategy 读取侧 ────────────────────────────
    def has_entry(self, pair_id: str) -> bool:
        """该 pair 是否下过单(execution 是否启动过)。"""
        return pair_id in self._settled

    def any_unsettled(self, pair_id: str) -> bool:
        """entry 存在且至少一腿仍为 false(健康检查兜底刷新的价值点;settled gate 的 fail-closed 触发条件)。"""
        legs = self._settled.get(pair_id)
        return legs is not None and not all(legs.values())

    def all_settled(self, pair_id: str) -> bool:
        """entry 存在且所有腿均 true。"""
        legs = self._settled.get(pair_id)
        return bool(legs) and all(legs.values())

    def has_any_unsettled(self) -> bool:
        """全局:任一 pair 有未结算腿(OE 健康检查状态维度兜底的触发条件,§6.8.3 Phase 2)。"""
        return any(not all(legs.values()) for legs in self._settled.values() if legs)
