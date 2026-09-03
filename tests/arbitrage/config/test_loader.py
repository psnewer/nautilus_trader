"""Slice 3:ArbConfig schema(msgspec)+ JSON loader + env 凭证注入。

对应用例:config-loader.{1-13}。
"""

import json
import warnings
from pathlib import Path

import pytest

from src.arbitrage.config import ArbConfig
from src.arbitrage.config import ConfigError
from src.arbitrage.config import DataSourcesConfig
from src.arbitrage.config import SharpExchSectionConfig
from src.arbitrage.config import SportsStatusDataSourceConfig
from src.arbitrage.config import StrategyJsonConfig
from src.arbitrage.config import load_arb_config
from src.arbitrage.config.loader import ConfigWarning


@pytest.fixture
def cfg_path(tmp_path):
    """给一个临时 path,各 test 自行 write_text 后 load。"""
    return tmp_path / "arb_config.json"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """删所有可能影响 loader 的 env,避免 test 互相污染或宿主机 .env 干扰。"""
    for var in [
        "ORBITEXCH_USERNAME", "ORBITEXCH_PASSWORD",
        "SHARPEXCH_USERNAME", "SHARPEXCH_PASSWORD",
        "POLYMARKET_CLOB_API_KEY", "POLYMARKET_CLOB_SECRET", "POLYMARKET_CLOB_PASSPHRASE",
        "POLYMARKET_SIGNATURE_TYPE", "POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER",
        "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_PASSPHRASE",
        "POLYMARKET_PROXY_URL", "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
    ]:
        monkeypatch.delenv(var, raising=False)


# ── .1 default JSON(空对象)→ ArbConfig 默认值 ─────────────────
def test_default_empty_json(cfg_path):
    cfg_path.write_text("{}")
    cfg = load_arb_config(cfg_path)
    assert isinstance(cfg, ArbConfig)
    assert cfg.arbitrage.share == 22.5
    assert cfg.venues.polymarket.clob_url == "https://clob.polymarket.com"
    assert cfg.venues.orbitexch.headless is True
    assert cfg.venues.sharpexch.enabled is False
    assert cfg.venues.sharpexch.base_url == "https://portal.sharpxch.com"
    assert cfg.debug is None


def test_execution_market_order_enabled_is_rejected(cfg_path):
    """市价语义已迁到 `place_bets.market`;旧全局字段必须 fail-fast。"""
    cfg_path.write_text(json.dumps({"execution": {"market_order_enabled": True}}))
    with pytest.raises(ConfigError, match="market_order_enabled"):
        load_arb_config(cfg_path)


def test_config_package_exports_current_schema_types():
    assert DataSourcesConfig.__name__ == "DataSourcesConfig"
    assert SportsStatusDataSourceConfig.__name__ == "SportsStatusDataSourceConfig"
    assert SharpExchSectionConfig.__name__ == "SharpExchSectionConfig"
    assert StrategyJsonConfig.__name__ == "StrategyJsonConfig"


# ── .2 JSON 全字段(关键路径)→ 全部解析 ─────────────────────
def test_full_json_parses(cfg_path):
    payload = {
        "discovery": {
            "refresh_interval_secs": 30,
            "polymarket": {"enabled": True, "sports": [{"sport": "Tennis", "competitions": ["atp"]}]},
            "orbitexch": {"enabled": True, "sports": [{"sport": "Tennis", "competitions": ["Men's Roland Garros 2026"]}]},
            "sharpexch": {"enabled": True, "sports": [{"sport": "Tennis", "competitions": ["Men's Wimbledon 2026"]}]},
        },
        "matching": {
            "competition_aliases": {"atp": "ATP", "Men's Roland Garros 2026": "ATP"},
        },
        "arbitrage": {
            "share": 50.0,
            "fx": 1.5,
            "max_leg_share": 75.0,
            "evaluate_on_depth_change": True,
        },
        "venues": {"sharpexch": {"enabled": True}},
        "risk": {"match_tp": 0.08, "min_probability": 0.04, "prob_buy_only": True},
        "strategy": {
            "strategies": {
                "tennis_prematch": {
                    "arbitrage_tree": {"self_hits": None, "sub_conditions": [], "checktion": {}, "actions": []},
                },
            },
            "bindings": [{"scope": "competition:ATP", "strategy_id": "tennis_prematch"}],
        },
    }
    cfg_path.write_text(json.dumps(payload))
    cfg = load_arb_config(cfg_path)
    assert cfg.discovery.polymarket.sports[0].sport == "Tennis"
    assert cfg.discovery.orbitexch.sports[0].competitions == ["Men's Roland Garros 2026"]
    assert cfg.discovery.sharpexch.sports[0].competitions == ["Men's Wimbledon 2026"]
    assert cfg.venues.sharpexch.enabled is True
    assert cfg.matching.competition_aliases["atp"] == "ATP"
    assert cfg.arbitrage.share == 50.0
    assert cfg.arbitrage.fx == 1.5
    assert cfg.arbitrage.max_leg_share == 75.0
    assert cfg.arbitrage.evaluate_on_depth_change is True
    assert cfg.risk.min_probability == 0.04
    assert cfg.risk.prob_buy_only is True
    assert cfg.strategy.bindings[0].scope == "competition:ATP"
    assert cfg.strategy.strategies["tennis_prematch"].arbitrage_tree is not None


def test_example_config_omits_data_sources_but_gets_defaults():
    """示例配置不重复写 PMSPORTS sports;schema 默认启用 data source 并继承 PM sports。"""
    cfg = load_arb_config(Path(__file__).parents[3] / "arb_config.example.json")

    assert cfg.data_sources.sports_status.enabled is True
    assert cfg.data_sources.sports_status.provider == "polymarket_sports"
    assert cfg.data_sources.sports_status.sports == []
    assert cfg.discovery.polymarket.sports[0].competitions == ["wimbledon"]


def test_risk_prob_buy_only_must_be_boolean(cfg_path):
    cfg_path.write_text(json.dumps({"risk": {"prob_buy_only": "true"}}))

    with pytest.raises(ConfigError, match="schema mismatch"):
        load_arb_config(cfg_path)


def test_unknown_risk_arbitrage_fields_raise_schema_mismatch(cfg_path):
    cfg_path.write_text(json.dumps({
        "risk": {"share": 50.0, "max_leg_share": 100.0, "fx": 1.5},
    }))
    with pytest.raises(ConfigError, match="schema mismatch.*unknown field `share`.*\\$\\.risk"):
        load_arb_config(cfg_path)


def test_legacy_global_sl_field_raises_schema_mismatch(cfg_path):
    cfg_path.write_text(json.dumps({"risk": {"global_sl": -0.10}}))
    with pytest.raises(ConfigError, match="schema mismatch.*unknown field `global_sl`.*\\$\\.risk"):
        load_arb_config(cfg_path)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("risk", "execution_enabled", False),
        ("risk", "health_check_interval_sec", 120.0),
        ("risk", "match_overrides", {}),
        ("venues.orbitexch", "discount", 1.0),
        ("venues.orbitexch", "take_off", 0.0),
        ("venues.orbitexch", "market_order_enabled", False),
        ("venues.orbitexch", "supported_market_types", ["home", "draw", "away"]),
        ("venues.sharpexch", "discount", 1.0),
        ("venues.sharpexch", "take_off", 0.0),
        ("venues.sharpexch", "market_order_enabled", False),
        ("venues.sharpexch", "supported_market_types", ["home", "draw", "away"]),
        ("venues.orbitexch", "api_url", "https://www.orbitexch.com/customer/api"),
        ("venues.orbitexch", "zoom_level", 0.8),
        ("venues.orbitexch", "page_refresh_sec", 600),
        ("venues.orbitexch", "cdp_url", "http://127.0.0.1:9222"),
        ("venues.orbitexch", "default_persistence", "LAPSE"),
        ("venues.orbitexch", "default_order_type", "GTC"),
        ("venues.sharpexch", "api_url", "https://portal.sharpxch.com/customer/api"),
        ("venues.sharpexch", "zoom_level", 0.8),
        ("venues.sharpexch", "page_refresh_sec", 600),
        ("venues.sharpexch", "cdp_url", "http://127.0.0.1:9222"),
        ("venues.sharpexch", "default_persistence", "LAPSE"),
        ("venues.sharpexch", "default_order_type", "GTC"),
        ("discovery", "enabled", True),
        ("matching", "enabled", True),
        ("risk", "enabled", True),
        ("execution", "enabled", True),
        ("execution", "tracking_check_interval_sec", 5.0),
        ("execution", "max_failure_retries", 5),
        ("execution", "staleness_timeout_sec", 300),
        ("venues.polymarket", "user_address", "0xuser"),
        ("venues.polymarket", "eoa_address", "0xeoa"),
        ("strategy", "signals", {}),
    ],
)
def test_legacy_dead_config_fields_raise_schema_mismatch(cfg_path, section, field, value):
    payload = {}
    cursor = payload
    parts = section.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor.setdefault(parts[-1], {})[field] = value

    cfg_path.write_text(json.dumps(payload))
    with pytest.raises(ConfigError, match=rf"schema mismatch.*unknown field `{field}`"):
        load_arb_config(cfg_path)


# ── .3 env 凭证注入 PM ──────────────────────────────────────────
def test_env_injects_polymarket_credentials(cfg_path, monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLOB_API_KEY", "test_key")
    monkeypatch.setenv("POLYMARKET_CLOB_SECRET", "test_secret")
    monkeypatch.setenv("POLYMARKET_CLOB_PASSPHRASE", "test_pass")
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "2")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xdead")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xfun")
    cfg_path.write_text("{}")
    cfg = load_arb_config(cfg_path)
    assert cfg.venues.polymarket.clob_api_key == "test_key"
    assert cfg.venues.polymarket.clob_api_secret == "test_secret"
    assert cfg.venues.polymarket.clob_passphrase == "test_pass"
    assert cfg.venues.polymarket.signature_type == 2
    assert cfg.venues.polymarket.private_key == "0xdead"
    assert cfg.venues.polymarket.funder == "0xfun"


def test_proxy_not_injected_from_env(cfg_path, monkeypatch):
    """#276:代理只从 JSON 读,env 不注入(未配置 = None = 直连)。"""
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7890")
    monkeypatch.setenv("POLYMARKET_PROXY_URL", "http://127.0.0.1:7890")
    cfg_path.write_text("{}")
    cfg = load_arb_config(cfg_path)
    assert cfg.venues.polymarket.proxy_url is None
    assert cfg.venues.orbitexch.proxy_url is None
    assert cfg.venues.sharpexch.proxy_url is None


def test_json_proxy_url_per_venue(cfg_path, monkeypatch):
    """#276:三 venue 对称的显式 proxy_url。"""
    monkeypatch.setenv("https_proxy", "http://env-proxy:7890")
    cfg_path.write_text(json.dumps({"venues": {
        "polymarket": {"proxy_url": "http://json-proxy:7890"},
        "orbitexch": {"proxy_url": "http://oe-proxy:7891"},
    }}))
    cfg = load_arb_config(cfg_path)
    assert cfg.venues.polymarket.proxy_url == "http://json-proxy:7890"
    assert cfg.venues.orbitexch.proxy_url == "http://oe-proxy:7891"
    assert cfg.venues.sharpexch.proxy_url is None


# ── .4 env 凭证注入 OE ──────────────────────────────────────────
def test_env_injects_orbitexch_credentials(cfg_path, monkeypatch):
    monkeypatch.setenv("ORBITEXCH_USERNAME", "oe_user")
    monkeypatch.setenv("ORBITEXCH_PASSWORD", "oe_pw")
    cfg_path.write_text("{}")
    cfg = load_arb_config(cfg_path)
    assert cfg.venues.orbitexch.username == "oe_user"
    assert cfg.venues.orbitexch.password == "oe_pw"


def test_env_injects_sharpexch_credentials(cfg_path, monkeypatch):
    monkeypatch.setenv("SHARPEXCH_USERNAME", "se_user")
    monkeypatch.setenv("SHARPEXCH_PASSWORD", "se_pw")
    cfg_path.write_text("{}")
    cfg = load_arb_config(cfg_path)
    assert cfg.venues.sharpexch.username == "se_user"
    assert cfg.venues.sharpexch.password == "se_pw"


# ── .5 env 缺失 → cfg 字段保 None ───────────────────────────────
def test_env_missing_keeps_none(cfg_path):
    cfg_path.write_text("{}")
    cfg = load_arb_config(cfg_path)
    assert cfg.venues.polymarket.clob_api_key is None
    assert cfg.venues.orbitexch.username is None
    assert cfg.venues.sharpexch.username is None


# ── .7 env 优先于 JSON 内同字段 ────────────────────────────────
def test_env_overrides_json_credential(cfg_path, monkeypatch):
    monkeypatch.setenv("ORBITEXCH_PASSWORD", "env_value")
    # JSON 含 password(本身就该 warn,但行为是覆盖)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConfigWarning)
        cfg_path.write_text(json.dumps({"venues": {"orbitexch": {"password": "json_value"}}}))
        cfg = load_arb_config(cfg_path)
    assert cfg.venues.orbitexch.password == "env_value"


def test_env_overrides_json_sharpexch_credential(cfg_path, monkeypatch):
    monkeypatch.setenv("SHARPEXCH_PASSWORD", "env_value")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConfigWarning)
        cfg_path.write_text(json.dumps({"venues": {"sharpexch": {"password": "json_value"}}}))
        cfg = load_arb_config(cfg_path)
    assert cfg.venues.sharpexch.password == "env_value"


# ── .8 JSON 含凭证 → ConfigWarning ─────────────────────────────
def test_credential_in_json_triggers_warning(cfg_path):
    cfg_path.write_text(json.dumps({"venues": {"polymarket": {"clob_api_key": "leaked"}}}))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConfigWarning)
        load_arb_config(cfg_path)
    assert any(issubclass(w.category, ConfigWarning) for w in caught)
    msg = str(caught[0].message)
    assert "venues.polymarket.clob_api_key" in msg


def test_sharpexch_credential_in_json_triggers_warning(cfg_path):
    cfg_path.write_text(json.dumps({"venues": {"sharpexch": {"username": "leaked"}}}))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConfigWarning)
        load_arb_config(cfg_path)
    assert any(issubclass(w.category, ConfigWarning) for w in caught)
    assert "venues.sharpexch.username" in str(caught[0].message)


# ── .9 干净 JSON(无凭证字段)→ 无 warning ─────────────────────
def test_clean_json_no_warning(cfg_path):
    cfg_path.write_text(json.dumps({"arbitrage": {"share": 10}}))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConfigWarning)
        load_arb_config(cfg_path)
    assert not any(issubclass(w.category, ConfigWarning) for w in caught)


# ── .10 文件不存在 → ConfigError ───────────────────────────────
def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_arb_config(tmp_path / "nonexistent.json")


# ── .11 无效 JSON → ConfigError ────────────────────────────────
def test_invalid_json_raises_config_error(cfg_path):
    cfg_path.write_text("{ not valid json")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_arb_config(cfg_path)


# ── .12 schema 字段类型错 → ConfigError ────────────────────────
def test_schema_mismatch_raises_config_error(cfg_path):
    cfg_path.write_text(json.dumps({"risk": {"match_tp": "not_a_number"}}))
    with pytest.raises(ConfigError, match="schema mismatch"):
        load_arb_config(cfg_path)


# ── .13 venues 段缺 → loader 补默认 section ─────────────────
def test_missing_venues_section_is_safe(cfg_path, monkeypatch):
    monkeypatch.setenv("ORBITEXCH_USERNAME", "u")
    cfg_path.write_text("{}")  # 整个 venues 段缺
    cfg = load_arb_config(cfg_path)
    # env 注入仍生效(loader 补出空 dict)
    assert cfg.venues.orbitexch.username == "u"


def test_venues_section_must_be_object(cfg_path):
    cfg_path.write_text(json.dumps({"venues": ["not-object"]}))
    with pytest.raises(ConfigError, match="config section venues must be JSON object"):
        load_arb_config(cfg_path)


def test_nested_venue_section_must_be_object(cfg_path):
    cfg_path.write_text(json.dumps({"venues": {"polymarket": ["not-object"]}}))
    with pytest.raises(ConfigError, match="config section venues.polymarket must be JSON object"):
        load_arb_config(cfg_path)


# ── root 非 object → ConfigError ─────────────────────────────
def test_root_must_be_object(cfg_path):
    cfg_path.write_text("[1,2,3]")
    with pytest.raises(ConfigError, match="must be JSON object"):
        load_arb_config(cfg_path)
