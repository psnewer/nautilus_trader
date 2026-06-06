"""Slice 4:ArbConfig → 各组件 config 的纯函数派发。

对应用例:config-dispatcher.{1-x}。
"""

import msgspec
import pytest

from src.arbitrage.config.dispatcher import to_arb_context_init_kwargs
from src.arbitrage.config.dispatcher import to_arb_risk_params
from src.arbitrage.config.dispatcher import to_debug_config
from src.arbitrage.config.dispatcher import to_market_matching_actor_config
from src.arbitrage.config.dispatcher import to_orbitexch_data_client_config
from src.arbitrage.config.dispatcher import to_orbitexch_exec_client_config
from src.arbitrage.config.dispatcher import to_polymarket_data_client_config
from src.arbitrage.config.dispatcher import to_polymarket_exec_client_config
from src.arbitrage.config.dispatcher import to_strategy_evaluator_config
from src.arbitrage.config.schema import ArbConfig
from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.debug.config import MockCategory


def _cfg(**overrides) -> ArbConfig:
    """构造 ArbConfig:overrides 提供整段 dict 替换。"""
    return msgspec.convert(overrides, type=ArbConfig)


# ─── Venues ───────────────────────────────────────────────────────────

def test_polymarket_data_client_config_maps_credentials():
    cfg = _cfg(venues={"polymarket": {
        "clob_url": "https://x.com", "ws_url": "wss://y.com",
        "clob_api_key": "K", "clob_api_secret": "S", "clob_passphrase": "P",
        "private_key": "0xpk", "funder": "0xfn",
    }})
    cc = to_polymarket_data_client_config(cfg)
    assert cc.api_key == "K"
    assert cc.api_secret == "S"
    assert cc.passphrase == "P"
    assert cc.private_key == "0xpk"
    assert cc.funder == "0xfn"
    assert cc.base_url_http == "https://x.com"
    assert cc.base_url_ws == "wss://y.com"


def test_polymarket_exec_client_config_maps_credentials():
    cfg = _cfg(venues={"polymarket": {"clob_api_key": "K"}})
    cc = to_polymarket_exec_client_config(cfg)
    assert cc.api_key == "K"


def test_polymarket_credentials_none_passthrough():
    cfg = _cfg()  # 全默认,凭证全 None
    cc = to_polymarket_data_client_config(cfg)
    assert cc.api_key is None
    assert cc.private_key is None


def test_orbitexch_data_client_config_maps_credentials():
    cfg = _cfg(venues={"orbitexch": {
        "username": "u", "password": "p", "headless": False, "browser_type": "firefox",
    }})
    cc = to_orbitexch_data_client_config(cfg)
    assert cc.username == "u"
    assert cc.password == "p"
    assert cc.headless is False
    assert cc.browser_type == "firefox"


def test_orbitexch_credentials_empty_string_fallback():
    """OE Config 把 username/password 标为必填 str;env 缺失时 dispatcher 回退空串
    让下游 login 触发明确错误(loader 不预判)。"""
    cfg = _cfg()  # username/password 全 None
    cc = to_orbitexch_data_client_config(cfg)
    assert cc.username == ""
    assert cc.password == ""


def test_orbitexch_exec_client_config_maps_credentials():
    cfg = _cfg(venues={"orbitexch": {"username": "u", "password": "p"}})
    cc = to_orbitexch_exec_client_config(cfg)
    assert cc.username == "u"
    assert cc.password == "p"


# ─── Actors ───────────────────────────────────────────────────────────

# #59(slice A):test_instrument_refresher_configs 删除 —— InstrumentRefresher 退役。


def test_market_matching_actor_config_maps_fields():
    cfg = _cfg(
        discovery={"refresh_interval_secs": 60.0},
        matching={"min_similarity": 1, "competition_max_matches": {"ATP": 1}},
    )
    mc = to_market_matching_actor_config(cfg)
    assert mc.refresh_interval_secs == 60.0
    assert mc.min_similarity == 1
    assert mc.competition_max_matches == {"ATP": 1}


def test_market_matching_empty_max_matches_becomes_none():
    """空 dict 不传入 MarketMatchingConfig(其默认是 None,内部 `or {}` 兜底)。"""
    cfg = _cfg()  # competition_max_matches 默认空 dict
    mc = to_market_matching_actor_config(cfg)
    assert mc.competition_max_matches is None


def test_strategy_evaluator_config_log_evaluations_default():
    cfg = _cfg()
    sc = to_strategy_evaluator_config(cfg)
    assert sc.log_evaluations is False


# ─── Risk / Context / Debug ───────────────────────────────────────────

def test_arb_risk_params_maps_fields():
    cfg = _cfg(risk={"share": 50.0, "fx": 1.5, "match_tp": 0.08,
                     "match_sl": -0.06, "global_sl": -0.20})
    rp = to_arb_risk_params(cfg)
    assert rp.share == 50.0
    assert rp.fx == 1.5
    assert rp.match_tp == 0.08
    assert rp.match_sl == -0.06
    assert rp.global_sl == -0.20


def test_arb_context_init_kwargs_maps_execution_section():
    cfg = _cfg(execution={"tracking_timeout_sec": 45.0,
                          "health_check_interval_sec": 90.0})
    kw = to_arb_context_init_kwargs(cfg)
    assert kw["pm_session_timeout_secs"] == 45.0
    assert kw["pm_health_interval_secs"] == 90.0
    assert kw["oe_session_timeout_secs"] == 45.0
    assert kw["oe_health_interval_secs"] == 90.0


# ── slice 7A:OE scraper config + aliases 进 ArbContext ────

def test_arb_context_init_kwargs_includes_oe_scraper_config():
    cfg = _cfg(
        discovery={"orbitexch": {"enabled": True,
                                  "sports": [{"sport": "Tennis", "competitions": ["Men's Roland Garros 2026"]}]}},
        venues={"orbitexch": {"headless": False, "page_load_timeout_sec": 90.0}},
    )
    kw = to_arb_context_init_kwargs(cfg)
    sc = kw["oe_scraper_config"]
    assert sc is not None
    assert sc.enabled is True
    # #62:scraper 强制 headless + 无登录 profile(免登录、后台定时、与 data/exec 登录浏览器解耦),
    # 不跟随 venue.headless(=False)
    assert sc.browser.headless is True
    assert sc.browser.user_data_dir is None
    assert sc.browser.timeout_ms == 90000   # 90s → 90000ms
    assert sc.sports[0].sport == "Tennis"
    assert sc.sports[0].competitions == ["Men's Roland Garros 2026"]


def test_arb_context_init_kwargs_oe_scraper_config_none_when_disabled():
    cfg = _cfg(discovery={"orbitexch": {"enabled": False}})
    kw = to_arb_context_init_kwargs(cfg)
    assert kw["oe_scraper_config"] is None


def test_arb_context_init_kwargs_includes_aliases():
    cfg = _cfg(matching={"sport_aliases": {"soccer": "Soccer"},
                         "competition_aliases": {"atp": "ATP",
                                                  "Men's Roland Garros 2026": "ATP"}})
    kw = to_arb_context_init_kwargs(cfg)
    assert kw["oe_sport_aliases"] == {"soccer": "Soccer"}
    assert kw["oe_competition_aliases"]["Men's Roland Garros 2026"] == "ATP"


# ── #55:PM 发现目标 + competition→sport map ──────────────────

def test_pm_event_slug_tags_from_competitions():
    cfg = _cfg(discovery={"polymarket": {"enabled": True,
                                          "sports": [{"sport": "Tennis", "competitions": ["atp", "wta"]}]}})
    kw = to_arb_context_init_kwargs(cfg)
    assert kw["pm_event_slug_tags"] == ["atp", "wta"]


def test_pm_event_slug_tags_empty_when_disabled():
    cfg = _cfg(discovery={"polymarket": {"enabled": False}})
    kw = to_arb_context_init_kwargs(cfg)
    assert kw["pm_event_slug_tags"] == []


def test_pm_competition_to_sport_map():
    cfg = _cfg(discovery={"polymarket": {"enabled": True,
                                          "sports": [{"sport": "Tennis", "competitions": ["atp", "wta"]},
                                                     {"sport": "Soccer", "competitions": ["epl"]}]}})
    kw = to_arb_context_init_kwargs(cfg)
    assert kw["pm_competition_to_sport"] == {"atp": "Tennis", "wta": "Tennis", "epl": "Soccer"}


def test_debug_config_none_when_section_missing():
    cfg = _cfg()  # debug 默认 None
    assert to_debug_config(cfg) is None


def test_debug_config_none_when_disabled():
    cfg = _cfg(debug={"enabled": False})
    assert to_debug_config(cfg) is None


def test_debug_config_enabled_maps_overrides_and_mock_data():
    cfg = _cfg(debug={
        "enabled": True,
        "overrides": {"skip_execution": {"enabled": True, "value": True, "description": "no real orders"}},
        "mock_data": {
            "force_odds": {
                "category": "odds", "name": "force_odds", "enabled": True,
                "data": {"price": 0.5}, "conditions": {"instrument_id": "X.PM"},
                "priority": 1,
            },
        },
    })
    dbg = to_debug_config(cfg)
    assert isinstance(dbg, DebugConfig)
    assert dbg.enabled is True
    assert dbg.is_override_active("skip_execution")
    assert dbg.get_override_value("skip_execution") is True

    mock = dbg.get_mock(MockCategory.ODDS, {"instrument_id": "X.PM"})
    assert mock is not None
    assert mock.data == {"price": 0.5}
    assert mock.priority == 1


def test_debug_config_conditions_not_match_returns_none():
    cfg = _cfg(debug={
        "enabled": True,
        "mock_data": {
            "force_odds": {
                "category": "odds", "enabled": True, "data": {"x": 1},
                "conditions": {"instrument_id": "X.PM"}, "priority": 0,
            },
        },
    })
    dbg = to_debug_config(cfg)
    assert dbg.get_mock(MockCategory.ODDS, {"instrument_id": "Y.OE"}) is None


def test_debug_config_pure_function_does_not_mutate_cfg():
    """`to_debug_config` 应是纯函数(`cfg` 是 frozen msgspec,验证未受影响)。"""
    cfg = _cfg(debug={"enabled": True,
                      "overrides": {"x": {"enabled": True, "value": 1}}})
    _ = to_debug_config(cfg)
    # cfg.debug.overrides 应保持原样(没被 dispatcher 改写)
    assert cfg.debug.overrides["x"]["value"] == 1
