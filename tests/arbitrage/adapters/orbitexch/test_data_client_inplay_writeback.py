"""Slice 9(#49):OE DataClient `write_inplay_to_instrument_info` helper(把 inplay 写到 `cache.instrument.info`)。

`_on_price_frame` 完整路径 NT 重(_cache 是 cdef readonly,Mock 困难)— 验证拆出的 helper 即可。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from nautilus_trader.adapters.orbitexch.data import write_inplay_to_instrument_info


def test_inplay_written_when_instrument_present():
    cache = MagicMock()
    inst = SimpleNamespace(info={"sport": "Tennis"})
    cache.instrument = MagicMock(return_value=inst)

    write_inplay_to_instrument_info(cache, "X.ORBITEXCH", True)
    assert inst.info["in_play"] is True
    assert inst.info["sport"] == "Tennis"   # 保留原 key


def test_inplay_false_written():
    cache = MagicMock()
    inst = SimpleNamespace(info={})
    cache.instrument = MagicMock(return_value=inst)

    write_inplay_to_instrument_info(cache, "X.ORBITEXCH", False)
    assert inst.info["in_play"] is False


def test_skip_when_instrument_missing():
    """cache.instrument 返 None(冷启动 / 路由表跟 cache 不同步)→ 不 raise。"""
    cache = MagicMock()
    cache.instrument = MagicMock(return_value=None)
    write_inplay_to_instrument_info(cache, "X.ORBITEXCH", True)   # 不 raise


def test_skip_when_info_missing():
    """instrument 存在但 info=None → 不 raise(防御性)。"""
    cache = MagicMock()
    inst = SimpleNamespace(info=None)
    cache.instrument = MagicMock(return_value=inst)
    write_inplay_to_instrument_info(cache, "X.ORBITEXCH", True)   # 不 raise
