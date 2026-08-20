"""StrategyEvaluator._update_price_trend:每帧 Δprob,按 instrument_id 分 venue/leg(#trend)。

`_update_price_trend` 只用 self.cache / self._price_last / self._price_trend,可用轻量 stand-in
直接调未绑定方法测,不必装配整个 NT Strategy。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.arbitrage.strategy.actor import StrategyEvaluator


def _book(ask):
    b = MagicMock()
    b.best_ask_price = MagicMock(return_value=ask)
    return b


class _Cache:
    def __init__(self, books):
        self._b = {str(k): v for k, v in books.items()}

    def order_book(self, iid):
        return self._b.get(str(iid))


def _fs(books):
    return SimpleNamespace(cache=_Cache(books), _price_last={}, _price_trend={})


def _feed(fs, iid, ask):
    fs.cache = _Cache({iid: _book(ask)})
    return StrategyEvaluator._update_price_trend(fs, SimpleNamespace(instrument_id=iid))


def test_first_frame_has_no_trend_only_seeds_last():
    fs = _fs({})
    assert _feed(fs, "Y.POLYMARKET", 0.40) is None
    assert "Y.POLYMARKET" not in fs._price_trend      # 首帧无 prev → 不算趋势
    assert fs._price_last["Y.POLYMARKET"] == pytest.approx(0.40)


def test_trend_is_delta_prob_and_carries_prev_across_frames():
    fs = _fs({})
    _feed(fs, "Y.POLYMARKET", 0.40)
    assert _feed(fs, "Y.POLYMARKET", 0.50) == pytest.approx(0.10)
    assert fs._price_trend["Y.POLYMARKET"] == pytest.approx(0.10)   # 概率变大 → 正
    assert _feed(fs, "Y.POLYMARKET", 0.45) == pytest.approx(-0.05)
    assert fs._price_trend["Y.POLYMARKET"] == pytest.approx(-0.05)  # 概率变小 → 负
    assert fs._price_last["Y.POLYMARKET"] == pytest.approx(0.45)    # last 已滚动到当前


def test_separates_by_instrument_id_venue_and_leg():
    fs = _fs({})
    # 同 pair 的两条腿 + 两个 venue,各自独立
    for iid, a0, a1 in [
        ("Y.POLYMARKET", 0.40, 0.44),
        ("N.POLYMARKET", 0.60, 0.55),
        ("H.ORBITEXCH", 0.30, 0.31),
    ]:
        _feed(fs, iid, a0)
        _feed(fs, iid, a1)
    assert fs._price_trend["Y.POLYMARKET"] == pytest.approx(0.04)
    assert fs._price_trend["N.POLYMARKET"] == pytest.approx(-0.05)
    assert fs._price_trend["H.ORBITEXCH"] == pytest.approx(0.01)


def test_unchanged_price_keeps_prior_trend_not_flat():
    # 纯深度帧(top-ask 价不变)不得把趋势冲成 flat,保留上次真实移动
    fs = _fs({})
    _feed(fs, "Y.POLYMARKET", 0.40)
    _feed(fs, "Y.POLYMARKET", 0.50)
    assert fs._price_trend["Y.POLYMARKET"] == pytest.approx(0.10)
    assert _feed(fs, "Y.POLYMARKET", 0.50) is None  # 纯深度帧不是当前价格变化
    assert fs._price_trend["Y.POLYMARKET"] == pytest.approx(0.10)   # 仍是上次的 +0.10,非 0
    assert fs._price_last["Y.POLYMARKET"] == pytest.approx(0.50)
    _feed(fs, "Y.POLYMARKET", 0.53)   # 再真实移动 → 从上次真实价 0.50 算
    assert fs._price_trend["Y.POLYMARKET"] == pytest.approx(0.03)


def test_unchanged_price_on_first_delta_seeds_no_trend():
    # 首帧后紧跟同价帧:仍无 prev-move,趋势不应被造出 0
    fs = _fs({})
    _feed(fs, "Y.POLYMARKET", 0.40)
    _feed(fs, "Y.POLYMARKET", 0.40)   # 价未变 → 直接返回,不写趋势
    assert "Y.POLYMARKET" not in fs._price_trend
    assert fs._price_last["Y.POLYMARKET"] == pytest.approx(0.40)


def test_skips_missing_or_nonpositive_best_ask():
    fs = _fs({})
    _feed(fs, "Y.POLYMARKET", 0.40)
    # 无 book → 跳过,不更新 last/trend
    fs.cache = _Cache({})
    StrategyEvaluator._update_price_trend(fs, SimpleNamespace(instrument_id="Y.POLYMARKET"))
    assert fs._price_last["Y.POLYMARKET"] == pytest.approx(0.40)   # 未被覆盖
    assert "Y.POLYMARKET" not in fs._price_trend


def test_none_instrument_id_noop():
    fs = _fs({})
    StrategyEvaluator._update_price_trend(fs, SimpleNamespace(instrument_id=None))
    assert fs._price_last == {}
    assert fs._price_trend == {}
