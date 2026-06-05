"""DebugConfig —— Q11.5 普通对象,DI 注入,不进 Cache。

对应用例:debug-config.{1-6}
"""

import json
import tempfile
from pathlib import Path

from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.debug.config import DebugOverride
from src.arbitrage.debug.config import MockCategory
from src.arbitrage.debug.config import MockDataItem


# ── config.1: 默认禁用,所有 override 都不激活 ───────────────────
def test_default_disabled_no_overrides_active():
    cfg = DebugConfig()
    assert cfg.enabled is False
    assert cfg.is_override_active("skip_check_size") is False
    assert cfg.get_override_value("skip_check_size", default=None) is None


# ── config.2: 总开关 + 该项 enabled 双闸 ─────────────────────────
def test_override_requires_both_enabled_flags():
    """总开关 OR 该项 enabled 任一为 False → 不激活。"""
    cfg = DebugConfig(enabled=True)
    cfg.overrides["skip_check_size"] = DebugOverride(name="skip_check_size", enabled=True, value=True)
    assert cfg.is_override_active("skip_check_size") is True

    # 该项 enabled = False → 不激活
    cfg.overrides["skip_check_size"].enabled = False
    assert cfg.is_override_active("skip_check_size") is False

    # 该项重启 + 总开关关 → 不激活
    cfg.overrides["skip_check_size"].enabled = True
    cfg.enabled = False
    assert cfg.is_override_active("skip_check_size") is False


# ── config.3: get_override_value 给 default 当未激活 ────────────
def test_get_override_value_falls_back_to_default():
    cfg = DebugConfig(enabled=True)
    cfg.overrides["polymarket_price"] = DebugOverride(name="polymarket_price", enabled=True, value=0.01)
    assert cfg.get_override_value("polymarket_price") == 0.01
    assert cfg.get_override_value("not_set", default="fallback") == "fallback"


# ── config.4: mock_data 按 category + conditions 匹配 ───────────
def test_mock_data_matches_by_category_and_conditions():
    cfg = DebugConfig(enabled=True)
    cfg.mock_data["m1"] = MockDataItem(
        id="m1", category=MockCategory.ODDS, enabled=True,
        data={"price": 2.0}, conditions={"market_id": "1-123"},
    )
    # 命中
    item = cfg.get_mock(MockCategory.ODDS, {"market_id": "1-123"})
    assert item is not None and item.data == {"price": 2.0}
    # conditions 不匹配 → None
    assert cfg.get_mock(MockCategory.ODDS, {"market_id": "other"}) is None
    # category 不匹配 → None
    assert cfg.get_mock(MockCategory.POSITIONS, {"market_id": "1-123"}) is None


# ── config.5: mock priority 高的赢 ──────────────────────────────
def test_mock_priority_higher_wins():
    cfg = DebugConfig(enabled=True)
    cfg.mock_data["lo"] = MockDataItem(
        id="lo", category=MockCategory.ODDS, enabled=True, data="LO", priority=1,
    )
    cfg.mock_data["hi"] = MockDataItem(
        id="hi", category=MockCategory.ODDS, enabled=True, data="HI", priority=10,
    )
    item = cfg.get_mock(MockCategory.ODDS)
    assert item is not None and item.data == "HI"


# ── config.6: 文件 load + roundtrip ──────────────────────────────
def test_load_from_json_file_roundtrip():
    src = {
        "enabled": True,
        "overrides": {"skip_check_size": {"enabled": True, "value": True}},
        "mock_data": {},
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(src, f)
        path = f.name
    try:
        cfg = DebugConfig.load(path)
        assert cfg.enabled is True
        assert cfg.is_override_active("skip_check_size") is True
        # 序列化回去结构稳定
        round_trip = cfg.to_dict()
        assert round_trip["enabled"] is True
        assert round_trip["overrides"]["skip_check_size"]["enabled"] is True
        assert round_trip["overrides"]["skip_check_size"]["value"] is True
    finally:
        Path(path).unlink()


# ── 边界:disabled 时所有 mock / override 都不激活 ──────────────
def test_disabled_blocks_everything():
    cfg = DebugConfig(enabled=False)
    cfg.overrides["any"] = DebugOverride(name="any", enabled=True, value=42)
    cfg.mock_data["any"] = MockDataItem(id="any", category=MockCategory.ODDS, enabled=True, data="x")
    assert cfg.is_override_active("any") is False
    assert cfg.get_mock(MockCategory.ODDS) is None
