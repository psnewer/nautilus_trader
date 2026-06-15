"""per-pair 机会串行闸(synchronization.md §7,#84)。

不变量:同一 pair 同时只有 1 笔套利在「评估 → 执行」生命周期内(不同 pair 可并发)。

补的是全局互斥(§1-6 `_execution_active` / `execution.*`)拦不住的洞:那些在飞信号在
**异步 `_submit_order` 下游**才置位,而 strategy 评估是并发 `create_task` 的 → 同一 OBD 突发的
多个评估在信号置位前各自 fire(同毫秒重复下单,见 refactor.md #82)。本闸在 **OBD 回调同步段**
(`create_task` 之前)置位,单 loop 串行保证后到的并发评估立刻看到 → 放弃。

所有 acquire/release 都必须在首个 `await` 之前同步调用(复用 §4 单 loop 无锁纪律)。本类纯内存、无时钟依赖。

**无兜底猜测(#105 ②)**:不再有 max-hold 陈旧自愈,也不再有健检 `clear_all`。in-flight 一定有出口,
**靠结构保证**:fire 后所有权要么进 execution(`exec_started`→`exec_finished`,由 session watchdog
兜到底),要么经 opportunity barrier 的 deny/timeout 出口 `release_eval` 释放;未 fire 由 strategy
`release_eval` 释放。任一执行 session 的 `exec_started` 与 watchdog 在 `_begin_session` 内原子置位,
保证 `exec_finished` 一定被走到(终态或超时),故 `_exec_count` 必回落到 0、in-flight 必被清。
"""

from __future__ import annotations


class PairInFlightGate:
    """per-pair「评估→执行」串行闸。

    生命周期:
    - strategy `_route_eval`(同步,`create_task` 前):`try_enter(pair)` → False 即放弃本次评估;
    - strategy `_evaluate_and_fire` 收尾:未 fire → `release_eval(pair)`;已 fire → 不释放(交执行 / barrier);
    - opportunity barrier deny/timeout 出口:`release_eval(pair)`(此刻 `exec_count==0`,腿被 barrier 扣着未下到 venue);
    - execution `_begin_session`(per-leg):`exec_started(pair)`;`_end_session`/timeout:`exec_finished(pair)`;
      per-pair session 计数归 0 → 释放 in-flight。
    """

    def __init__(self) -> None:
        self._inflight: set[str] = set()         # 在飞 pair_id(评估中 / 执行中)
        self._exec_count: dict[str, int] = {}    # pair_id → 在飞 execution session 数

    def try_enter(self, pair_id: str) -> bool:
        """评估入口同步 acquire。已在飞 → False(放弃);否则置位 → True。"""
        if pair_id in self._inflight:
            return False
        self._inflight.add(pair_id)
        return True

    def release_eval(self, pair_id: str) -> None:
        """strategy 未 fire(无机会 / abort / 异常)或 barrier deny/timeout → 释放。
        fire 后已进执行(exec_count>0)则 no-op(交 `exec_finished` 清)。"""
        if self._exec_count.get(pair_id, 0) == 0:
            self._inflight.discard(pair_id)

    def exec_started(self, pair_id: str) -> None:
        """execution session 启动(per-leg):per-pair 计数 ++(套利已 fire,所有权进入执行)。"""
        self._exec_count[pair_id] = self._exec_count.get(pair_id, 0) + 1

    def exec_finished(self, pair_id: str) -> None:
        """execution session 结束(terminal/timeout):计数 --;归 0 → 释放 in-flight。"""
        n = self._exec_count.get(pair_id, 0) - 1
        if n <= 0:
            self._exec_count.pop(pair_id, None)
            self._inflight.discard(pair_id)
        else:
            self._exec_count[pair_id] = n

    def is_in_flight(self, pair_id: str) -> bool:
        """只读探测(测试 / 诊断用)。"""
        return pair_id in self._inflight
