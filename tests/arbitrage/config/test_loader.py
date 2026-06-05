"""Slice 3:ArbConfig schema(msgspec)+ JSON loader + env 凭证注入。

对应用例:config-loader.{1-13}。
"""

import json
import warnings
from pathlib import Path

import pytest

from src.arbitrage.config import ArbConfig
from src.arbitrage.config import ConfigError
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
        "POLYMARKET_CLOB_API_KEY", "POLYMARKET_CLOB_SECRET", "POLYMARKET_CLOB_PASSPHRASE",
        "POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER",
        "POLYMARKET_USER_ADDRESS", "POLYMARKET_ADDRESS", "POLYMARKET_EOA_ADDRESS",
        "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_PASSPHRASE",
    ]:
        monkeypatch.delenv(var, raising=False)


# ── .1 default JSON(空对象)→ ArbConfig 默认值 ─────────────────
def test_default_empty_json(cfg_path):
    cfg_path.write_text("{}")
    cfg = load_arb_config(cfg_path)
    assert isinstance(cfg, ArbConfig)
    assert cfg.discovery.enabled is True
    assert cfg.risk.share == 22.5
    assert cfg.venues.polymarket.clob_url == "https://clob.polymarket.com"
    assert cfg.venues.orbitexch.headless is True
    assert cfg.debug is None


# ── .2 JSON 全字段(关键路径)→ 全部解析 ─────────────────────
def test_full_json_parses(cfg_path):
    payload = {
        "discovery": {
            "enabled": True,
            "refresh_interval_secs": 30,
            "polymarket": {"enabled": True, "sports": [{"sport": "Tennis", "competitions": ["atp"]}]},
            "orbitexch": {"enabled": True, "sports": [{"sport": "Tennis", "competitions": ["Men's Roland Garros 2026"]}]},
        },
        "matching": {
            "min_similarity": 1,
            "competition_aliases": {"atp": "ATP", "Men's Roland Garros 2026": "ATP"},
        },
        "risk": {"share": 50.0, "fx": 1.5, "match_tp": 0.08},
        "strategy": {
            "strategies": {
                "tennis_prematch": {
                    "arbitrage_tree": {"self_hits": None, "sub_conditions": [], "checktion": [], "action": None},
                },
            },
            "bindings": [{"scope": "competition:ATP", "strategy_id": "tennis_prematch"}],
        },
    }
    cfg_path.write_text(json.dumps(payload))
    cfg = load_arb_config(cfg_path)
    assert cfg.discovery.polymarket.sports[0].sport == "Tennis"
    assert cfg.discovery.orbitexch.sports[0].competitions == ["Men's Roland Garros 2026"]
    assert cfg.matching.competition_aliases["atp"] == "ATP"
    assert cfg.risk.share == 50.0
    assert cfg.strategy.bindings[0].scope == "competition:ATP"
    assert cfg.strategy.strategies["tennis_prematch"].arbitrage_tree is not None


# ── .3 env 凭证注入 PM ──────────────────────────────────────────
def test_env_injects_polymarket_credentials(cfg_path, monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLOB_API_KEY", "test_key")
    monkeypatch.setenv("POLYMARKET_CLOB_SECRET", "test_secret")
    monkeypatch.setenv("POLYMARKET_CLOB_PASSPHRASE", "test_pass")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xdead")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xfun")
    monkeypatch.setenv("POLYMARKET_USER_ADDRESS", "0xuser")
    monkeypatch.setenv("POLYMARKET_EOA_ADDRESS", "0xeoa")
    cfg_path.write_text("{}")
    cfg = load_arb_config(cfg_path)
    assert cfg.venues.polymarket.clob_api_key == "test_key"
    assert cfg.venues.polymarket.clob_api_secret == "test_secret"
    assert cfg.venues.polymarket.clob_passphrase == "test_pass"
    assert cfg.venues.polymarket.private_key == "0xdead"
    assert cfg.venues.polymarket.funder == "0xfun"
    assert cfg.venues.polymarket.user_address == "0xuser"
    assert cfg.venues.polymarket.eoa_address == "0xeoa"


# ── .4 env 凭证注入 OE ──────────────────────────────────────────
def test_env_injects_orbitexch_credentials(cfg_path, monkeypatch):
    monkeypatch.setenv("ORBITEXCH_USERNAME", "oe_user")
    monkeypatch.setenv("ORBITEXCH_PASSWORD", "oe_pw")
    cfg_path.write_text("{}")
    cfg = load_arb_config(cfg_path)
    assert cfg.venues.orbitexch.username == "oe_user"
    assert cfg.venues.orbitexch.password == "oe_pw"


# ── .5 env 缺失 → cfg 字段保 None ───────────────────────────────
def test_env_missing_keeps_none(cfg_path):
    cfg_path.write_text("{}")
    cfg = load_arb_config(cfg_path)
    assert cfg.venues.polymarket.clob_api_key is None
    assert cfg.venues.orbitexch.username is None


# ── .6 `POLYMARKET_ADDRESS` 别名 fallback ──────────────────────
def test_user_address_alias_fallback(cfg_path, monkeypatch):
    monkeypatch.setenv("POLYMARKET_ADDRESS", "0xalias")
    # POLYMARKET_USER_ADDRESS 不设
    cfg_path.write_text("{}")
    cfg = load_arb_config(cfg_path)
    assert cfg.venues.polymarket.user_address == "0xalias"


def test_user_address_primary_wins_over_alias(cfg_path, monkeypatch):
    monkeypatch.setenv("POLYMARKET_USER_ADDRESS", "0xprimary")
    monkeypatch.setenv("POLYMARKET_ADDRESS", "0xalias")
    cfg_path.write_text("{}")
    cfg = load_arb_config(cfg_path)
    assert cfg.venues.polymarket.user_address == "0xprimary"


# ── .7 env 优先于 JSON 内同字段 ────────────────────────────────
def test_env_overrides_json_credential(cfg_path, monkeypatch):
    monkeypatch.setenv("ORBITEXCH_PASSWORD", "env_value")
    # JSON 含 password(本身就该 warn,但行为是覆盖)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConfigWarning)
        cfg_path.write_text(json.dumps({"venues": {"orbitexch": {"password": "json_value"}}}))
        cfg = load_arb_config(cfg_path)
    assert cfg.venues.orbitexch.password == "env_value"


# ── .8 JSON 含凭证 → ConfigWarning ─────────────────────────────
def test_credential_in_json_triggers_warning(cfg_path):
    cfg_path.write_text(json.dumps({"venues": {"polymarket": {"clob_api_key": "leaked"}}}))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConfigWarning)
        load_arb_config(cfg_path)
    assert any(issubclass(w.category, ConfigWarning) for w in caught)
    msg = str(caught[0].message)
    assert "venues.polymarket.clob_api_key" in msg


# ── .9 干净 JSON(无凭证字段)→ 无 warning ─────────────────────
def test_clean_json_no_warning(cfg_path):
    cfg_path.write_text(json.dumps({"risk": {"share": 10}}))
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
    cfg_path.write_text(json.dumps({"risk": {"share": "not_a_number"}}))
    with pytest.raises(ConfigError, match="schema mismatch"):
        load_arb_config(cfg_path)


# ── .13 venues 段缺 → loader setdefault 兜底 ─────────────────
def test_missing_venues_section_is_safe(cfg_path, monkeypatch):
    monkeypatch.setenv("ORBITEXCH_USERNAME", "u")
    cfg_path.write_text("{}")  # 整个 venues 段缺
    cfg = load_arb_config(cfg_path)
    # env 注入仍生效(loader setdefault 出空 dict)
    assert cfg.venues.orbitexch.username == "u"


# ── root 非 object → ConfigError ─────────────────────────────
def test_root_must_be_object(cfg_path):
    cfg_path.write_text("[1,2,3]")
    with pytest.raises(ConfigError, match="must be JSON object"):
        load_arb_config(cfg_path)
