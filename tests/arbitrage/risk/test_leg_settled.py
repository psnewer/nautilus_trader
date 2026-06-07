"""risk-6.9.13: LegSettledRegistry 共享对象语义。"""

from src.arbitrage.common.leg_settled import LegSettledRegistry


def test_absent_entry_does_not_trigger_gate():
    r = LegSettledRegistry()
    assert not r.has_entry("p1")
    assert not r.any_unsettled("p1")   # entry 不存在 → 不触发 settled gate
    assert not r.all_settled("p1")


def test_reset_marks_all_unsettled():
    r = LegSettledRegistry()
    r.reset("p1", ["A.PM", "B.OE"])
    assert r.has_entry("p1")
    assert r.any_unsettled("p1")       # 全 false
    assert not r.all_settled("p1")


def test_mark_each_leg_until_all_settled():
    r = LegSettledRegistry()
    r.reset("p1", ["A.PM", "B.OE"])
    r.mark("p1", "A.PM")
    assert r.any_unsettled("p1")       # 还有一腿 false
    r.mark("p1", "B.OE")
    assert not r.any_unsettled("p1")
    assert r.all_settled("p1")


def test_mark_nonexistent_entry_is_ignored():
    r = LegSettledRegistry()
    r.mark("p1", "A.PM")               # 非 execution 触发不创建 entry
    assert not r.has_entry("p1")


def test_mark_unknown_instrument_is_ignored():
    r = LegSettledRegistry()
    r.reset("p1", ["A.PM", "B.OE"])
    r.mark("p1", "Z.OE")               # 不在本轮腿集合,忽略,不崩
    assert r.any_unsettled("p1")


def test_has_any_unsettled_global():
    """§6.8.3 Phase 2:全局任一 pair 有未结算腿 → True(OE 健康检查状态维度触发条件)。"""
    r = LegSettledRegistry()
    assert not r.has_any_unsettled()           # 无 entry
    r.reset("p1", ["A.PM", "B.OE"])
    assert r.has_any_unsettled()               # p1 全 false
    r.mark("p1", "A.PM")
    assert r.has_any_unsettled()               # p1 还有一腿 false
    r.mark("p1", "B.OE")
    assert not r.has_any_unsettled()           # p1 全 settled,无其它 entry
    r.reset("p2", ["C.PM"])
    assert r.has_any_unsettled()               # p2 未结算
