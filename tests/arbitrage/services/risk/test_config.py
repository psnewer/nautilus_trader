"""
RiskConfig 单元测试
"""

from src.arbitrage.services.risk.config import RiskConfig


# =========================================================================
# 默认值
# =========================================================================


def test_default_config():
    config = RiskConfig()
    assert config.enabled is True
    assert config.execution_enabled is True
    assert config.match_sl == -0.10
    assert config.global_sl == -0.50
    assert config.match_tp == 0.10
    assert config.match_overrides == {}


# =========================================================================
# 序列化 / 反序列化
# =========================================================================


def test_to_dict_contains_all_fields():
    config = RiskConfig(execution_enabled=False, match_sl=-0.20)
    d = config.to_dict()
    assert d["enabled"] is True
    assert d["execution_enabled"] is False
    assert d["match_sl"] == -0.20
    assert d["global_sl"] == -0.50
    assert d["match_tp"] == 0.10
    assert "match_overrides" in d


def test_from_dict_roundtrip():
    original = RiskConfig(
        enabled=False,
        execution_enabled=False,
        match_sl=-0.15,
        global_sl=-0.80,
        match_tp=0.25,
        match_overrides={"pair-1": -0.05},
    )
    restored = RiskConfig.from_dict(original.to_dict())
    assert restored.enabled == original.enabled
    assert restored.execution_enabled == original.execution_enabled
    assert restored.match_sl == original.match_sl
    assert restored.global_sl == original.global_sl
    assert restored.match_tp == original.match_tp
    assert restored.match_overrides == original.match_overrides


def test_from_dict_with_missing_fields_uses_defaults():
    config = RiskConfig.from_dict({})
    assert config.enabled is True
    assert config.execution_enabled is True
    assert config.match_sl == -0.10


def test_from_dict_execution_enabled_defaults_true():
    """旧配置文件不包含 execution_enabled 字段时默认为 True"""
    config = RiskConfig.from_dict({"enabled": True, "match_sl": -0.10})
    assert config.execution_enabled is True


# =========================================================================
# get_match_sl 覆盖
# =========================================================================


def test_get_match_sl_default():
    config = RiskConfig(match_sl=-0.10)
    assert config.get_match_sl("pair-1") == -0.10


def test_get_match_sl_with_override():
    config = RiskConfig(
        match_sl=-0.10,
        match_overrides={"pair-1": -0.05, "pair-2": -0.20},
    )
    assert config.get_match_sl("pair-1") == -0.05
    assert config.get_match_sl("pair-2") == -0.20
    assert config.get_match_sl("pair-3") == -0.10  # 未覆盖的使用默认值
