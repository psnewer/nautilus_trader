"""PairRegistry —— 唯一写者 MatchingActor,只读 consumers risk/portfolio/session/strategy。

跨组件契约(P11,主方=matching);#34 修正:取代之前 risk 错误地用 info["competition"] 当 pair_id。
"""

from src.arbitrage.common.pair_registry import PairRegistry


def test_get_unknown_returns_none():
    """matching-3.pair.1: 未注册 instrument → None(下游 gate 自然不触发,不崩)。"""
    r = PairRegistry()
    assert r.get("A.PM") is None
    assert len(r) == 0


def test_register_maps_all_legs_to_same_pair_id():
    """matching-3.pair.2: 一次 register 把整组腿(PM+OE)映射到同一 pair_id。"""
    r = PairRegistry()
    r.register("EPL|Arsenal|Chelsea", ["A_home.PM", "A_away.PM", "X_home.OE", "X_away.OE"])
    assert r.get("A_home.PM") == "EPL|Arsenal|Chelsea"
    assert r.get("X_away.OE") == "EPL|Arsenal|Chelsea"
    assert r.all_pair_ids() == {"EPL|Arsenal|Chelsea"}
    assert len(r) == 4


def test_register_same_pair_again_idempotent_overwrite():
    """matching-3.pair.3: 同 pair_id 重 register 覆盖(重匹配场景),不重复累加。"""
    r = PairRegistry()
    pid = "EPL|A|B"
    r.register(pid, ["L1", "L2"])
    r.register(pid, ["L1", "L2"])           # 重匹配
    assert len(r) == 2 and r.get("L1") == pid


def test_register_drops_stale_legs_for_same_pair():
    """matching-3.pair.4: 重 register 同 pair_id 但腿集合变了 → 旧腿映射清除(罕见但安全)。"""
    r = PairRegistry()
    pid = "EPL|A|B"
    r.register(pid, ["L1", "L2", "L3"])
    r.register(pid, ["L1", "L2"])           # L3 不再属此 pair
    assert r.get("L3") is None
    assert r.get("L1") == pid and r.get("L2") == pid
    assert r.instrument_ids_for_pair(pid) == {"L1", "L2"}


def test_instrument_ids_for_pair_returns_registered_legs():
    """matching-3.pair.4b: pair→legs 反查供 Portfolio 读取完整 outcome 集合。"""
    r = PairRegistry()
    r.register("p1", ["L1", "L2"])
    r.register("p2", ["L3"])
    assert r.instrument_ids_for_pair("p1") == {"L1", "L2"}
    assert r.instrument_ids_for_pair("unknown") == set()


def test_anchor_ids_are_registered_but_not_returned_as_tradable_legs_by_default():
    """matching-3.pair.4c(#127):PMSPORTS anchor id 单独存,默认不暴露给套利消费者。"""
    r = PairRegistry()
    r.register("p1", ["L1", "L2"], anchor_instrument_ids=["777.PMSPORTS"])

    assert r.get("777.PMSPORTS") == "p1"
    assert r.instrument_ids_for_pair("p1") == {"L1", "L2"}
    assert r.instrument_ids_for_pair("p1", tradable_only=False) == {"L1", "L2", "777.PMSPORTS"}
    assert r.anchor_ids_for_pair("p1") == {"777.PMSPORTS"}


def test_register_same_pair_drops_stale_anchor_ids():
    """matching-3.pair.4d(#127):重匹配时 anchor 映射也覆盖旧集合。"""
    r = PairRegistry()
    r.register("p1", ["L1"], anchor_instrument_ids=["old.PMSPORTS"])
    r.register("p1", ["L1"], anchor_instrument_ids=["new.PMSPORTS"])

    assert r.get("old.PMSPORTS") is None
    assert r.get("new.PMSPORTS") == "p1"


def test_unregister_pair_clears_all_legs():
    """matching-3.pair.5: unregister_pair 清掉该 pair 所有腿(测试 / 重匹配显式清理用)。"""
    r = PairRegistry()
    r.register("EPL|A|B", ["L1", "L2"])
    r.register("EPL|C|D", ["L3", "L4"])
    r.unregister_pair("EPL|A|B")
    assert r.get("L1") is None and r.get("L2") is None
    assert r.get("L3") == "EPL|C|D"          # 其它 pair 不受影响


def test_multiple_pairs_isolated():
    """matching-3.pair.6: 多 pair 隔离,all_pair_ids 反映全部。"""
    r = PairRegistry()
    r.register("p1", ["L1", "L2"])
    r.register("p2", ["L3"])
    assert r.all_pair_ids() == {"p1", "p2"} and len(r) == 3


# ── 控制台热改 refresh_interval(#119,方案乙 consumer)──────────────────
from src.arbitrage.matching.actor import MarketMatchingActor
from src.arbitrage.common.control import SetRefreshIntervalCommand


def _bare_matching_actor(interval=30.0):
    a = MarketMatchingActor.__new__(MarketMatchingActor)
    a._refresh_interval_secs = interval
    return a


def test_refresh_interval_command_hot_updates():
    a = _bare_matching_actor(30.0)
    a._on_set_refresh_interval_cmd(SetRefreshIntervalCommand(secs=15.0))
    assert a._refresh_interval_secs == 15.0


def test_refresh_interval_command_rejects_nonpositive():
    a = _bare_matching_actor(30.0)
    a._on_set_refresh_interval_cmd(SetRefreshIntervalCommand(secs=0.0))
    a._on_set_refresh_interval_cmd(SetRefreshIntervalCommand(secs=-5.0))
    assert a._refresh_interval_secs == 30.0  # 非正值不 apply
