"""per-pair 评估串行闸(synchronization.md §7,#84;#261 收窄为 strategy 单层)。

不变量:**同一 pair 同时只有 1 次评估在跑**(不同 pair 可并发)。

补的洞:strategy 评估是并发 `create_task` 的,而所有下游在飞信号(session / barrier ctx)都在
**异步 `_submit_order` 下游**才置位 —— 同一 OBD 突发的多个评估会在信号出现前各自 fire(同毫秒
重复下单,见 refactor.md #82)。本闸在 **OBD 回调同步段**(`create_task` 之前)置位,单 loop 串行
保证后到的并发评估立刻看到 → 放弃。

**#261:本闸不再进入执行段。** 原先它跨 strategy / barrier / session 三个组件传递所有权,是
**持有型 token** —— 每个漏掉的 release 都是永久泄漏(#260 查出四类同源问题)。现在:

- 生命周期 **恰好等于评估 task 的生命周期**:`_dispatch_eval` `try_enter` ↔ `_on_eval_done`
  **无条件** `release`。没有跨组件交接,就没有"该不该释放"的判据,也就没有可漏的出口。
- **全局 ≤1 执行**改由 `ArbLiveExecutionEngine` barrier 单点判定,只读派生态
  (`_arb_opportunities` 非墓碑 ctx + 各 client `_execution_active`),无 token、无出口。

所有 acquire/release 都必须在首个 `await` 之前同步调用(复用 §4 单 loop 无锁纪律)。
本类纯内存、无时钟依赖、无兜底猜测。
"""

from __future__ import annotations


class PairInFlightGate:
    """per-pair 评估串行闸。

    生命周期(**加锁与释放同层对称**,均在 `_dispatch_eval` 这一层;协程内不碰闸):
    - strategy `_dispatch_eval`(同步,`create_task` 前):`try_enter(pair)` → False 即放弃本次评估;
    - strategy `_on_eval_done`(该 task 的 done-callback,**唯一释放点**):`release(pair)`,
      无条件 —— 正常返回 / 抛异常 / 被取消都释放;
    - 同层的排程失败分支(`_create_task` 抛)也在此层 `release` —— 协程从未排程,释放安全。
    """

    def __init__(self) -> None:
        self._inflight: set[str] = set()         # 正在评估的 pair_id

    def try_enter(self, pair_id: str) -> bool:
        """评估入口同步 acquire。已在评估 → False(放弃);否则置位 → True。"""
        if pair_id in self._inflight:
            return False
        self._inflight.add(pair_id)
        return True

    def release(self, pair_id: str) -> None:
        """评估 task 结束 → 无条件释放(#261:不再有"已 fire 就交给执行"的例外)。"""
        self._inflight.discard(pair_id)

    def is_in_flight(self, pair_id: str) -> bool:
        """只读探测(测试 / 诊断用)。"""
        return pair_id in self._inflight
