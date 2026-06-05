"""
OpportunitySnapshot —— Q20 机会快照:evaluate 开跑时一次性冻 per-pair 数据,
整轮 condition 树评估 + Action 决策都用同一份,免受期间新成交扰动。

冻什么(Q20 锁定):
- `order_books` per instrument(避免评估期间行情变动扰乱机会计算)
- `positions` per instrument
- `way_rebate`(portfolio.way_rebate 不在 cache,取那一刻调一次冻结果)

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
    way_rebate: dict = field(default_factory=dict)          # portfolio.way_rebate(pair_id) 那一刻的返水率
    # slice 9(#49):比赛进行中标志,从 OE leg `instrument.info["in_play"]` 派生(OE WS 帧带 inPlay,
    # 由 OE DataClient 写入 cache.instrument.info)。任一 OE leg in_play=True → pair in_play。
    in_play: bool = False
    # slice 9(#49):per-instrument info 冻结副本(每 iid → cache.instrument.info dict 浅拷贝)。
    # Check/Action 经 snapshot 读 `selection_role` / 其他 6-key,**不直接调 cache**(decouple)。
    instrument_info: dict = field(default_factory=dict)


def build_snapshot(
    pair_id: str,
    *,
    cache,
    portfolio,
    pair_registry,
) -> OpportunitySnapshot:
    """evaluator 开跑时调一次,返回 per-pair 快照。

    `cache` / `portfolio` 必须支持:
    - `cache.order_book(instrument_id)` → OrderBook(或 None)
    - `cache.positions_open(...)` → list[Position]
    - `portfolio.way_rebate(pair_id)` → dict[direction, rebate](已 settled-gate'd)

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
    way_rebate = portfolio.way_rebate(pair_id) if hasattr(portfolio, "way_rebate") else {}
    # in_play 派生 + instrument_info 冻结(slice 9 / #49):一次遍历搞两件
    in_play = False
    instrument_info: dict = {}
    for iid in instrument_ids:
        inst = cache.instrument(iid_objs[iid])
        info = getattr(inst, "info", None) if inst is not None else None
        if info:
            instrument_info[iid] = dict(info)  # 浅拷贝 freeze(后续 cache 写不影响)
            if info.get("in_play"):
                in_play = True
        else:
            instrument_info[iid] = {}
    return OpportunitySnapshot(
        pair_id=pair_id,
        instrument_ids=list(instrument_ids),
        order_books=order_books,
        positions=positions,
        way_rebate=way_rebate,
        in_play=in_play,
        instrument_info=instrument_info,
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
