"""OpportunitySnapshot —— Q20 per-pair 快照(evaluate 开跑时一次性冻)。

对应用例:strategy-4.framework.snap.{1-3}
"""

from types import SimpleNamespace

from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.strategy.snapshot import OpportunitySnapshot
from src.arbitrage.strategy.snapshot import build_snapshot


class _FakeInstrument:
    """带 mutable `info` dict 的伪 instrument(snapshot.in_play 派生 slice 9 / #49 用)。"""

    def __init__(self, info=None, *, min_quantity=None, min_notional=None, size_increment=None):
        self.info = dict(info or {})
        self.min_quantity = min_quantity
        self.min_notional = min_notional
        self.size_increment = size_increment


class _FakeCache:
    """实现 build_snapshot 需要的 method:order_book + positions_open + instrument(slice 9 加)。"""

    def __init__(self):
        self._books = {}
        self._positions = []
        self._instruments: dict = {}    # slice 9:in_play 派生需要 cache.instrument(iid).info

    # key 按 str 归一:真 cache 以 InstrumentId 键;build_snapshot 在 cache 边界传 InstrumentId,
    # 故查询/存储统一 str(iid) 以兼容测试用的 str id 与生产的 InstrumentId。
    def set_book(self, iid, book):
        self._books[str(iid)] = book

    def set_instrument(
        self,
        iid,
        info=None,
        *,
        min_quantity=None,
        min_notional=None,
        size_increment=None,
    ):
        """slice 9:setup `info["in_play"]` 等用。"""
        self._instruments[str(iid)] = _FakeInstrument(
            info=info,
            min_quantity=min_quantity,
            min_notional=min_notional,
            size_increment=size_increment,
        )

    def add_position(self, pos):
        self._positions.append(pos)

    def order_book(self, instrument_id):
        return self._books.get(str(instrument_id))

    def positions_open(self, **kwargs):
        return list(self._positions)

    def instrument(self, instrument_id):
        return self._instruments.get(str(instrument_id))


# ── snap.1: build_snapshot 一次性收集 per-pair 数据 ─────────────
def test_build_snapshot_collects_pair_state():
    cache = _FakeCache()
    cache.set_book("A.PM", "book-A")
    cache.set_book("B.OE", "book-B")
    cache.set_book("C.PM", "book-C")  # 别的 pair
    cache.add_position(SimpleNamespace(instrument_id="A.PM"))
    cache.add_position(SimpleNamespace(instrument_id="B.OE"))
    cache.add_position(SimpleNamespace(instrument_id="C.PM"))  # 别的 pair

    pair_registry = PairRegistry()
    pair_registry.register("match_X", ["A.PM", "B.OE"])
    pair_registry.register("match_Y", ["C.PM"])

    snap = build_snapshot("match_X", cache=cache, portfolio=SimpleNamespace(), pair_registry=pair_registry)

    assert snap.pair_id == "match_X"
    assert set(snap.instrument_ids) == {"A.PM", "B.OE"}
    assert snap.order_books == {"A.PM": "book-A", "B.OE": "book-B"}
    # positions 只取本 pair 的
    assert {p.instrument_id for p in snap.positions} == {"A.PM", "B.OE"}


# ── snap.2: 快照内容不被快照后的 cache 更新扰动(关键不变量)──
def test_snapshot_contents_isolated_from_subsequent_cache_changes():
    """snap.2: build_snapshot 返回后,cache 新写入不影响 snapshot.order_books / positions。

    本框架层只保证容器内容不变(dict 拷贝、list 拷贝);具体值的"深拷贝"(避免 OrderBook
    内部状态被改)由具体 Action 实现按需提取关键字段为 primitives。
    """
    cache = _FakeCache()
    cache.set_book("A.PM", "book-A-v1")
    cache.add_position(SimpleNamespace(instrument_id="A.PM", size=10))
    pair_registry = PairRegistry(); pair_registry.register("match_X", ["A.PM"])

    snap = build_snapshot("match_X", cache=cache, portfolio=SimpleNamespace(), pair_registry=pair_registry)

    # 取了 snapshot 之后,cache 再被改 / portfolio 返回不同值
    cache.set_book("A.PM", "book-A-v2")        # 行情变了
    cache.add_position(SimpleNamespace(instrument_id="A.PM", size=20))  # 新成交

    # snapshot 内容容器层面不变(dict / list 已拷贝,引用快照那一刻的值)
    assert snap.order_books == {"A.PM": "book-A-v1"}
    assert len(snap.positions) == 1            # 只一条(快照时的)


# ── snap.3: snapshot 可被 GC(无长存 dict,绑 per-evaluation 上下文)─
def test_snapshot_is_local_object_no_global_registry():
    """snap.3: build_snapshot 不向任何全局 dict 注册;返回的对象 evaluation 结束即可 GC。"""
    cache = _FakeCache()
    pair_registry = PairRegistry(); pair_registry.register("match_X", [])
    snap = build_snapshot("match_X", cache=cache, portfolio=SimpleNamespace(), pair_registry=pair_registry)
    # 没有模块级 dict 持有它(本测试通过观察:builder 模块层不暴露 snapshots 集合)
    import src.arbitrage.strategy.snapshot as snap_module
    assert not hasattr(snap_module, "_snapshots")
    assert not hasattr(snap_module, "snapshots")


# ── 边界:pair_id 没注册到 PairRegistry → 空 snapshot,不抛 ──
def test_unknown_pair_id_yields_empty_snapshot():
    cache = _FakeCache()
    pair_registry = PairRegistry()  # 空
    snap = build_snapshot("missing", cache=cache, portfolio=SimpleNamespace(), pair_registry=pair_registry)
    assert snap.instrument_ids == [] and snap.order_books == {} and snap.positions == []


def test_snapshot_freezes_instrument_minimums_for_order_planning():
    cache = _FakeCache()
    cache.set_instrument(
        "A.POLYMARKET",
        {"selection_role": "home", "min_buy_notional": 1.0},
        min_quantity=_QtyLike(5.0),
        min_notional=_QtyLike(7.0),
        size_increment=_QtyLike(0.01),
    )
    pair_registry = PairRegistry(); pair_registry.register("p", ["A.POLYMARKET"])

    snap = build_snapshot("p", cache=cache, portfolio=SimpleNamespace(), pair_registry=pair_registry)

    assert snap.instrument_constraints["A.POLYMARKET"] == {
        "min_quantity": 5.0,
        "min_notional": 7.0,
        "min_buy_notional": 1.0,
        "size_increment": 0.01,
    }


class _QtyLike:
    def __init__(self, value):
        self.value = value

    def as_double(self):
        return self.value


def test_snapshot_uses_tradable_pair_ids_not_anchor_ids():
    """strategy-pmsports-anchor.2:PMSPORTS anchor id 不进入机会快照。"""
    cache = _FakeCache()
    cache.set_book("A.POLYMARKET", "book-A")
    cache.set_book("X.ORBITEXCH", "book-X")
    cache.set_book("anchor.PMSPORTS", "book-anchor")
    cache.set_instrument("A.POLYMARKET", {})
    cache.set_instrument("X.ORBITEXCH", {})
    cache.set_instrument("anchor.PMSPORTS", {"tradable": False, "anchor": True})
    cache.add_position(SimpleNamespace(instrument_id="A.POLYMARKET"))
    cache.add_position(SimpleNamespace(instrument_id="anchor.PMSPORTS"))
    pair_registry = PairRegistry()
    pair_registry.register(
        "match_X",
        ["A.POLYMARKET", "X.ORBITEXCH"],
        anchor_instrument_ids=["anchor.PMSPORTS"],
    )

    snap = build_snapshot("match_X", cache=cache, portfolio=SimpleNamespace(), pair_registry=pair_registry)

    assert set(snap.instrument_ids) == {"A.POLYMARKET", "X.ORBITEXCH"}
    assert snap.order_books == {"A.POLYMARKET": "book-A", "X.ORBITEXCH": "book-X"}
    assert {p.instrument_id for p in snap.positions} == {"A.POLYMARKET"}
    assert "anchor.PMSPORTS" not in snap.instrument_info


# ── #250:in_play 单源 sports_state(旧 instrument.info["in_play"] 派生已废除)──

def test_snapshot_ignores_legacy_instrument_info_in_play():
    """旧 `info["in_play"]` 字段不再参与派生:即使为 True,无 sports state → in_play=False。"""
    cache = _FakeCache()
    cache.set_book("A.PM", "b"); cache.set_book("B.OE", "b")
    cache.set_instrument("A.PM", {})
    cache.set_instrument("B.OE", {"in_play": True})           # 遗留字段,忽略
    pair_registry = PairRegistry(); pair_registry.register("p", ["A.PM", "B.OE"])

    snap = build_snapshot("p", cache=cache, portfolio=SimpleNamespace(), pair_registry=pair_registry)
    assert snap.in_play is False


def test_snapshot_safe_when_instrument_missing_from_cache():
    """cache.instrument(iid) 返 None(冷启动 instrument 尚未入 cache)→ 不 raise,该 leg 跳过。"""
    cache = _FakeCache()
    cache.set_book("A.PM", "b")
    pair_registry = PairRegistry(); pair_registry.register("p", ["A.PM"])

    snap = build_snapshot("p", cache=cache, portfolio=SimpleNamespace(), pair_registry=pair_registry)
    assert snap.in_play is False  # 默认


# ── #250:sports_state 从 Store 冻结 + in_play 派生 ─────────────────────────
class _MemCache:
    def __init__(self):
        self.data = {}
    def add(self, key, value):
        self.data[key] = value
    def get(self, key):
        return self.data.get(key)


def _sports_store():
    from nautilus_trader.adapters.polymarket.sports import SportsGameStateStore

    return SportsGameStateStore(_MemCache())


def _sports_update(game_id=888, *, ts=1, score="0-0", live=False, ended=False):
    from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate

    return SportsGameUpdate(
        ts_event=ts, ts_init=ts, game_id=game_id, league="x", home_team="A", away_team="B",
        status="", score=score, period="", elapsed="", live=live, ended=ended, finished_ts="",
    )


def _sports_harness(*, info_in_play=False, game_id=888):
    cache = _FakeCache()
    cache.set_instrument("A.PM", {"game_id": game_id, "in_play": info_in_play})
    reg = PairRegistry()
    reg.register("match_X", ["A.PM"], game_id=game_id)
    return cache, reg


def test_snapshot_sports_state_frozen_from_store():
    """strategy-4.sports.4:评估中 Store 更新不改写本轮 sports_state;下一轮读到新值。"""
    cache, reg = _sports_harness()
    store = _sports_store()
    store.put(_sports_update(ts=1, score="0-0", live=False))

    snap = build_snapshot("match_X", cache=cache, portfolio=SimpleNamespace(),
                          pair_registry=reg, sports_store=store)
    store.put(_sports_update(ts=2, score="1-0", live=True))   # 评估期间新状态到达

    assert snap.sports_state.score == "0-0" and snap.sports_state.live is False
    snap2 = build_snapshot("match_X", cache=cache, portfolio=SimpleNamespace(),
                           pair_registry=reg, sports_store=store)
    assert snap2.sports_state.score == "1-0" and snap2.in_play is True


def test_snapshot_in_play_from_sports_state_only():
    """strategy-4.sports.5:in_play 单源 sports_state —— live 或 ended 为真 → True;
    无 sports state → False(旧 info["in_play"] 不参与)。"""
    store = _sports_store()

    cache, reg = _sports_harness(info_in_play=False)
    store.put(_sports_update(live=True))
    assert build_snapshot("match_X", cache=cache, portfolio=SimpleNamespace(),
                          pair_registry=reg, sports_store=store).in_play is True

    store.put(_sports_update(ts=2, live=False, ended=True))
    assert build_snapshot("match_X", cache=cache, portfolio=SimpleNamespace(),
                          pair_registry=reg, sports_store=store).in_play is True

    # sports 有状态但 live/ended 均 False → False
    store2 = _sports_store()
    store2.put(_sports_update(live=False, ended=False))
    cache2, reg2 = _sports_harness(info_in_play=True)   # 遗留字段在场也忽略
    assert build_snapshot("match_X", cache=cache2, portfolio=SimpleNamespace(),
                          pair_registry=reg2, sports_store=store2).in_play is False

    # 无 sports state → False
    cache3, reg3 = _sports_harness(info_in_play=True, game_id=666)
    snap = build_snapshot("match_X", cache=cache3, portfolio=SimpleNamespace(),
                          pair_registry=reg3, sports_store=_sports_store())
    assert snap.in_play is False and snap.sports_state is None


def test_snapshot_without_game_id_or_store_has_no_sports_state():
    """无 game_id 腿 / 未注入 store → sports_state None、in_play False(无覆盖即赛前假定)。"""
    cache = _FakeCache()
    cache.set_instrument("A.PM", {"in_play": True})   # 无 game_id;遗留字段忽略
    reg = PairRegistry()
    reg.register("match_X", ["A.PM"])

    snap = build_snapshot("match_X", cache=cache, portfolio=SimpleNamespace(),
                          pair_registry=reg, sports_store=_sports_store())
    assert snap.sports_state is None and snap.in_play is False

    snap_no_store = build_snapshot("match_X", cache=cache, portfolio=SimpleNamespace(), pair_registry=reg)
    assert snap_no_store.sports_state is None and snap_no_store.in_play is False
