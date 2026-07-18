"""
OpportunitySnapshot —— Q20 机会快照:evaluate 开跑时一次性冻 per-pair 数据,
整轮 condition 树评估 + Action 决策都用同一份,免受期间新成交扰动。

冻什么(Q20 锁定):
- `order_books` per instrument(避免评估期间行情变动扰乱机会计算)
- `positions` per instrument

**不冻什么**(走 live):
- `_execution_active`(Q19 同步;每次查必须最新)
- safety gate(settled / RiskEngine 余额检查;RiskEngine 端走 live)
- `PairRegistry` 查询(matching 写入瞬时同步,不冻)

**生命周期**:绑 per-evaluation 上下文(`EvalContext.snapshot`),evaluate + fire 结束 GC。
不长存 `self._snapshots` dict(refactor.md #21b 锁定)。
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field


@dataclass
class OpportunitySnapshot:
    """Q20 per-pair 快照。字段一次性赋值后不再变(消费侧只读)。

    `order_books` 字段值由 builder 决定(深拷贝 / 提取关键字段 / 引用快照接口),
    具体 Check/Action 实现 + Action 决策需要时按需细化;本框架只保证容器。
    """

    pair_id: str
    instrument_ids: list = field(default_factory=list)
    order_books: dict = field(default_factory=dict)         # instrument_id → OrderBook 视图(builder 决定具体形态)
    positions: list = field(default_factory=list)           # 该 pair 所有腿的 cache Position 引用 / 拷贝
    # 比赛进行中标志。#250:单源 `sports_state.live/ended`(旧 instrument.info["in_play"] 路径
    # 已废除);无 sports state 时为 False(覆盖判定待新架构下重新实现)。
    in_play: bool = False
    # #250:PMSPORTS 实时状态冻结视图(SportsGameStateStore 反序列化重建的 SportsGameUpdate;
    # 无状态时 None)。只读;不是交易腿,不进 order_books/positions/instrument_constraints/候选 legs。
    sports_state: object = None
    # slice 9(#49):per-instrument info 冻结副本(每 iid → cache.instrument.info dict 浅拷贝)。
    # Check/Action 经 snapshot 读 `selection_role` / 其他 matching key,**不直接调 cache**(decouple)。
    instrument_info: dict = field(default_factory=dict)
    # PlaceBets 在同一快照内做订单转换时使用的 NT 最小下单约束。
    # 只冻结规划需要的标量,不把 live Instrument 对象泄漏给 Action。
    instrument_constraints: dict = field(default_factory=dict)
    # pair 内 binary outcome 统一为 yes/no；selection_role 仅保留匹配/展示语义。
    outcomes: list = field(default_factory=lambda: ["yes", "no"])


def build_snapshot(
    pair_id: str,
    *,
    cache,
    portfolio,
    pair_registry,
    sports_store=None,
) -> OpportunitySnapshot:
    """evaluator 开跑时调一次,返回 per-pair 快照。

    `cache` / `portfolio` 必须支持:
    - `cache.order_book(instrument_id)` → OrderBook(或 None)
    - `cache.positions_open(...)` → list[Position]

    `pair_registry` 给 instrument_ids ← pair_id(matching 注册的;PairRegistry 反查实现)。

    **快照内容形态**:本 framework 保持容器中性,具体 `OrderBook` 是 NT 标准对象(返回 cache
    内部引用)—— Action 真正消费时如需"评估期不被新事件扰动",由 Action 实现层做提取(取
    best bid/ask 等基本量为 primitives)或调用 NT 标准复制 API。
    """
    instrument_ids = _pair_instrument_ids(pair_registry, pair_id)  # registry 原生 str(leg.id)
    # 公开字段(instrument_ids / order_books key)保留 str 视图;cache 查询边界转 InstrumentId。
    iid_objs = {iid: _as_instrument_id(iid) for iid in instrument_ids}
    order_books = {iid: cache.order_book(iid_objs[iid]) for iid in instrument_ids}
    # position.instrument_id 是 InstrumentId(live);经 str() 归一与 registry str id 比对。
    id_set = set(instrument_ids)
    positions = [p for p in cache.positions_open() if str(getattr(p, "instrument_id", None)) in id_set]
    # instrument_info 冻结(slice 9 / #49)+ outcomes 派生(#228):一次遍历
    instrument_info: dict = {}
    instrument_constraints: dict = {}
    for iid in instrument_ids:
        inst = cache.instrument(iid_objs[iid])
        info = getattr(inst, "info", None) if inst is not None else None
        if info:
            instrument_info[iid] = dict(info)  # 浅拷贝 freeze(后续 cache 写不影响)
        else:
            instrument_info[iid] = {}
        instrument_constraints[iid] = {
            "min_quantity": _object_value(getattr(inst, "min_quantity", None)),
            "min_notional": _object_value(getattr(inst, "min_notional", None)),
            "min_buy_notional": _object_value((info or {}).get("min_buy_notional")),
            "size_increment": _object_value(getattr(inst, "size_increment", None)),
        }
    # #250:按腿 info["game_id"] 从 SportsGameStateStore 冻结实时状态(store.get 每次反序列化,
    # 天然是本轮私有副本);in_play 单源 sports_state.live/ended,无状态即 False。
    sports_state = None
    if sports_store is not None:
        game_id = next(
            (info.get("game_id") for info in instrument_info.values() if info.get("game_id") is not None),
            None,
        )
        if game_id is not None:
            try:
                sports_state = sports_store.get(game_id)
            except Exception:  # noqa: BLE001 — store 读失败视为无状态,不挡评估
                sports_state = None
    in_play = sports_state is not None and bool(sports_state.live or sports_state.ended)
    return OpportunitySnapshot(
        pair_id=pair_id,
        instrument_ids=list(instrument_ids),
        order_books=order_books,
        positions=positions,
        in_play=in_play,
        instrument_info=instrument_info,
        instrument_constraints=instrument_constraints,
        outcomes=["yes", "no"],
        sports_state=sports_state,
    )


def _pair_instrument_ids(pair_registry, pair_id: str) -> list:
    """从 PairRegistry 反查该 pair 的所有腿 instrument_id。

    `PairRegistry` 是 `dict[instrument_id, pair_id]`(matching 写),反查需扫描。
    本框架不假设 PairRegistry 有专门的反向索引;扫描成本低(进程内 dict,几百条量级)。
    """
    if hasattr(pair_registry, "_by_instrument"):
        return [iid for iid, pid in pair_registry._by_instrument.items() if pid == pair_id]
    return []


def _as_instrument_id(iid):
    """registry 存 str(leg.id);NT cache.order_book/instrument 需 `InstrumentId` → 边界转型。"""
    from nautilus_trader.model.identifiers import InstrumentId

    return iid if isinstance(iid, InstrumentId) else InstrumentId.from_str(str(iid))


def _object_value(value) -> float | None:
    if value is None:
        return None
    for method in ("as_double", "as_decimal"):
        fn = getattr(value, method, None)
        if callable(fn):
            return float(fn())
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
