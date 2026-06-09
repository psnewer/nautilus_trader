"""per-pair 机会串行闸(synchronization.md §7,#84)。

不变量:同一 pair 同时只有 1 笔套利在「评估 → 执行」生命周期内(不同 pair 可并发)。

补的是全局互斥(§1-6 `_execution_active` / `execution.*`)拦不住的洞:那些在飞信号在
**异步 `_submit_order` 下游**才置位,而 strategy 评估是并发 `create_task` 的 → 同一 OBD 突发的
多个评估在信号置位前各自 fire(同毫秒重复下单,见 refactor.md #82)。本闸在 **OBD 回调同步段**
(`create_task` 之前)置位,单 loop 串行保证后到的并发评估立刻看到 → 放弃。

所有 acquire/release 都必须在首个 `await` 之前同步调用(复用 §4 单 loop 无锁纪律)。
本类纯内存、无时钟依赖:`try_enter` 由调用方传入 `now_ns` + `max_hold_ns`(max-hold 自愈兜底)。
"""

from __future__ import annotations


class PairInFlightGate:
    """per-pair「评估→执行」串行闸。

    生命周期:
    - strategy `_route_eval`(同步,`create_task` 前):`try_enter(pair)` → False 即放弃本次评估;
    - strategy `_evaluate_and_fire` 收尾:未 fire → `release_eval(pair)`;已 fire → 不释放(交执行);
    - execution `_begin_session`(per-leg):`exec_started(pair)`;`_end_session`:`exec_finished(pair)`;
      per-pair session 计数归 0 → 释放 in-flight。

    防泄漏:fire 了但一腿 session 都没起(全 deny / cancel-only 丢弃)→ in-flight 无人清 →
    `try_enter` 的 **max-hold 陈旧自愈**(in-flight 超 `max_hold_ns` 即视为空闲)兜底。
    """

    def __init__(self) -> None:
        self._inflight: dict[str, int] = {}      # pair_id → 置位时刻 ns(用于 max-hold 自愈)
        self._exec_count: dict[str, int] = {}    # pair_id → 在飞 execution session 数

    def try_enter(self, pair_id: str, now_ns: int, max_hold_ns: int) -> bool:
        """评估入口同步 acquire。已在飞且未超 max_hold → False(放弃);否则置位 → True。

        in-flight 超 `max_hold_ns`(> 单笔套利最长耗时,取 ≥2×tracking_timeout)视为陈旧泄漏 →
        覆盖重入(罕见兜底:正常路径由 release_eval / exec_finished 清)。"""
        ts = self._inflight.get(pair_id)
        if ts is not None and (now_ns - ts) < max_hold_ns:
            return False
        self._inflight[pair_id] = now_ns
        return True

    def release_eval(self, pair_id: str) -> None:
        """strategy 未 fire(无机会 / abort / 异常)→ 释放。fire 后 exec_count>0 则 no-op(交执行清)。"""
        if self._exec_count.get(pair_id, 0) == 0:
            self._inflight.pop(pair_id, None)

    def exec_started(self, pair_id: str) -> None:
        """execution session 启动(per-leg):per-pair 计数 ++(套利已 fire,所有权进入执行)。"""
        self._exec_count[pair_id] = self._exec_count.get(pair_id, 0) + 1

    def exec_finished(self, pair_id: str) -> None:
        """execution session 结束(terminal/timeout):计数 --;归 0 → 释放 in-flight。"""
        n = self._exec_count.get(pair_id, 0) - 1
        if n <= 0:
            self._exec_count.pop(pair_id, None)
            self._inflight.pop(pair_id, None)
        else:
            self._exec_count[pair_id] = n

    def is_in_flight(self, pair_id: str) -> bool:
        """只读探测(测试 / 诊断用);不带 max-hold 判断。"""
        return pair_id in self._inflight
