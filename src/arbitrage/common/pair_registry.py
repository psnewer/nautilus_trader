"""
PairRegistry —— 跨组件共享的 instrument_id → pair_id 映射。

**唯一写者**:`MarketMatchingActor`(匹配成功 → `register(pair_id, [所有腿 instrument_ids])`)。
**读者**:`risk._resolve_pair_id` / `portfolio._resolve_pair_id` / `session._pair_id_for` /
strategy(opportunity scan)。

#34 修正:`info["competition"]` 是**联赛名**(EPL/NFL/...),**不是** pair_id;pair_id 由
matching 算法产出。本注册表是 matching → 下游的桥。

P11 归属:有单一自然归属(matching 唯一写、其它只读)→ 放 matching 主方小节 + 交叉引用,
不单独成横切章节(详细设计 `architectures/matching/architecture.md §3.1`)。

**单进程单 asyncio loop,纯内存,无序列化**(同 `LegSettledRegistry` 模式)。
"""

from __future__ import annotations

from typing import Hashable


class PairRegistry:
    """`dict[instrument_id, pair_id]` —— 进程级 instrument→pair 映射。线程不安全(单 loop 串行)。"""

    def __init__(self) -> None:
        self._by_instrument: dict[Hashable, str] = {}

    # ── matching 写入侧 ──────────────────────────────────────────────
    def register(self, pair_id: str, instrument_ids) -> None:
        """匹配成功后把该 pair 的所有腿 instrument_id 映射到同一 pair_id。

        幂等:同 pair_id 再 register 时,新腿集合覆盖旧映射(也清理旧腿——若该腿不再属于
        此 pair)。重匹配场景安全。
        """
        # key 归一到 str:matching 写 `str(leg.id)`,但消费者(risk/portfolio/session)用
        # `InstrumentId` 对象查 → 边界统一 str(InstrumentId) 才能命中(否则 dict 恒 miss)。
        new_set = {str(iid) for iid in instrument_ids}
        # 清掉之前属于该 pair_id 但本轮不再包含的腿(罕见:重匹配后腿集合变化)
        stale = [iid for iid, pid in self._by_instrument.items() if pid == pair_id and iid not in new_set]
        for iid in stale:
            del self._by_instrument[iid]
        # 注册本轮所有腿
        for iid in new_set:
            self._by_instrument[iid] = pair_id

    def unregister_pair(self, pair_id: str) -> None:
        """清除该 pair 所有腿映射(可选,主要给重匹配 / 测试用)。"""
        stale = [iid for iid, pid in self._by_instrument.items() if pid == pair_id]
        for iid in stale:
            del self._by_instrument[iid]

    # ── consumers 读取侧 ─────────────────────────────────────────────
    def get(self, instrument_id: Hashable) -> str | None:
        """返回该 instrument 所属 pair_id;未注册返 None(下游 settled gate 自然不触发)。

        查询侧同样 `str(instrument_id)` 归一 —— 消费者多传 `InstrumentId` 对象,而 key 以 str 存。
        """
        return self._by_instrument.get(str(instrument_id))

    def all_pair_ids(self) -> set[str]:
        return set(self._by_instrument.values())

    def __len__(self) -> int:
        return len(self._by_instrument)
