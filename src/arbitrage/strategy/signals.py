"""
SignalStore —— 双状态信号容器(Q21 / Q9)。

- **persistent**:写后保留(如 `live=True`,比赛开始信号到 → set,后续读不重算;结束 → clear)
- **transient**:**用后即清**(如 `rebate`,赔率到 → 算 → set;Action 读 → `get` 消费,
  下次赔率到才重算)

读法两道:
- `peek(key)`:**不消费**——`BoolExpr` 求值用(求值不能因为看一眼就把信号清掉)
- `get(key)`:**消费 transient**——Action 内部决策用(确保用的是新鲜值,不会拿过期 stale)

同 key 同时存在 transient + persistent 时,`get` 优先消费 transient(避免读到 stale)。
"""

from __future__ import annotations


class SignalStore:
    """进程内信号存储,单 asyncio loop 串行访问(线程不安全)。"""

    def __init__(self) -> None:
        self._persistent: dict[str, object] = {}
        self._transient: dict[str, object] = {}

    # ── persistent(写后保留;比赛级状态)─────────────────────────────
    def set_persistent(self, key: str, value: object) -> None:
        self._persistent[key] = value

    def clear_persistent(self, key: str) -> None:
        self._persistent.pop(key, None)

    # ── transient(用后即清;每事件重算的瞬时信号)────────────────────
    def set_transient(self, key: str, value: object) -> None:
        self._transient[key] = value

    # ── 读:peek(不消费)/ get(消费 transient)──────────────────────
    def peek(self, key: str) -> object | None:
        """求值用:不消费 transient。优先看 transient(更新鲜),fallback persistent。"""
        if key in self._transient:
            return self._transient[key]
        return self._persistent.get(key)

    def get(self, key: str) -> object | None:
        """决策用:消费 transient(只读一次,确保不会用过期值)。persistent 不消费。"""
        if key in self._transient:
            return self._transient.pop(key)
        return self._persistent.get(key)

    # ── 内省 ─────────────────────────────────────────────────────────
    def has(self, key: str) -> bool:
        return key in self._transient or key in self._persistent

    # ── per-pair 隔离视图(slice 9 / #49,P3 方案)─────────────────────
    def view(self, pair_id: str) -> "_PairScopedStoreView":
        """返 per-pair 子视图:所有读写自动 namespace 到 `{key}.{pair_id}`。

        evaluator 构造 `EvalContext` 时传 `store=self._signal_store.view(pair_id)` —
        Check / SignalRef / signal_collector 写法不变,自动隔离不同 pair 的持久态。

        跨 pair 全局信号(罕见;如系统级 user_pause)直接用 root store 不走 view。
        """
        return _PairScopedStoreView(self, pair_id)


class _PairScopedStoreView:
    """SignalStore 的 per-pair 视图:thin wrapper,key 内部自动加 `.{pair_id}` 后缀。

    用例:
      view = store.view("pair_123")
      view.set_persistent("in_play", True)  # 实际写入 store._persistent["in_play.pair_123"]
      view.peek("in_play")                   # 实际读 store._persistent["in_play.pair_123"]
    """

    def __init__(self, base: SignalStore, pair_id: str) -> None:
        self._base = base
        self._pair_id = pair_id

    def _k(self, key: str) -> str:
        return f"{key}.{self._pair_id}"

    def set_persistent(self, key: str, value: object) -> None:
        self._base.set_persistent(self._k(key), value)

    def clear_persistent(self, key: str) -> None:
        self._base.clear_persistent(self._k(key))

    def set_transient(self, key: str, value: object) -> None:
        self._base.set_transient(self._k(key), value)

    def peek(self, key: str) -> object | None:
        return self._base.peek(self._k(key))

    def get(self, key: str) -> object | None:
        return self._base.get(self._k(key))

    def has(self, key: str) -> bool:
        return self._base.has(self._k(key))
