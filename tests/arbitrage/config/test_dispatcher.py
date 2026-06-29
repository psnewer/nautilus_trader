"""Slice 4:ArbConfig → 各组件 config 的纯函数派发。

对应用例:config-dispatcher.{1-x}。
"""

import msgspec
import pytest

from src.arbitrage.config.dispatcher import to_arb_context_init_kwargs
from src.arbitrage.config.dispatcher import to_arb_risk_params
from src.arbitrage.config.dispatcher import to_arbitrage_params
from src.arbitrage.config.dispatcher import to_debug_config
from src.arbitrage.config.dispatcher import to_market_matching_actor_config
from src.arbitrage.config.dispatcher import to_orbitexch_data_client_config
from src.arbitrage.config.dispatcher import to_orbitexch_exec_client_config
from src.arbitrage.config.dispatcher import to_polymarket_data_client_config
from src.arbitrage.config.dispatcher import to_polymarket_exec_client_config
from src.arbitrage.config.dispatcher import to_strategy_evaluator_config
from src.arbitrage.config.dispatcher import _polymarket_ws_base_url
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
        "proxy_url": "http://127.0.0.1:7890",
        "clob_api_key": "K", "clob_api_secret": "S", "clob_passphrase": "P",
        "signature_type": 2, "private_key": "0xpk", "funder": "0xfn",
    }})
    cc = to_polymarket_data_client_config(cfg)
    assert cc.api_key == "K"
    assert cc.api_secret == "S"
    assert cc.passphrase == "P"
    assert cc.signature_type == 2
    assert cc.private_key == "0xpk"
    assert cc.funder == "0xfn"
    assert cc.base_url_http == "https://x.com"
    assert cc.base_url_ws == "wss://y.com/"
    assert cc.proxy_url == "http://127.0.0.1:7890"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("wss://ws-subscriptions-clob.polymarket.com/ws/", "wss://ws-subscriptions-clob.polymarket.com/ws/"),
        ("wss://ws-subscriptions-clob.polymarket.com/ws", "wss://ws-subscriptions-clob.polymarket.com/ws/"),
        ("wss://ws-subscriptions-clob.polymarket.com/ws/market", "wss://ws-subscriptions-clob.polymarket.com/ws/"),
        ("wss://ws-subscriptions-clob.polymarket.com/ws/user", "wss://ws-subscriptions-clob.polymarket.com/ws/"),
    ],
)
def test_polymarket_ws_url_normalized_to_nt_base_url(raw, expected):
    """配置层兼容旧 full endpoint,传给上游 Polymarket WS client 前转为 base URL。"""
    assert _polymarket_ws_base_url(raw) == expected


def test_polymarket_exec_client_config_maps_credentials():
    cfg = _cfg(venues={"polymarket": {
        "clob_api_key": "K",
        "ws_url": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
    }})
    cc = to_polymarket_exec_client_config(cfg)
    assert cc.api_key == "K"
    assert cc.signature_type == 0
    assert cc.base_url_ws == "wss://ws-subscriptions-clob.polymarket.com/ws/"


def test_polymarket_exec_client_config_maps_signature_type():
    cfg = _cfg(venues={"polymarket": {"signature_type": 2}})
    cc = to_polymarket_exec_client_config(cfg)
    assert cc.signature_type == 2


def test_polymarket_exec_client_config_maps_proxy():
    cfg = _cfg(venues={"polymarket": {"proxy_url": "http://127.0.0.1:7890"}})
    cc = to_polymarket_exec_client_config(cfg)
    assert cc.proxy_url == "http://127.0.0.1:7890"


def test_polymarket_exec_client_config_maps_retry_params():
    cfg = _cfg(venues={"polymarket": {
        "max_retries": 2,
        "retry_delay_initial_ms": 500,
        "retry_delay_max_ms": 2_000,
    }})
    cc = to_polymarket_exec_client_config(cfg)
    assert cc.max_retries == 2
    assert cc.retry_delay_initial_ms == 500
    assert cc.retry_delay_max_ms == 2_000


def test_polymarket_exec_client_config_retry_params_default_none():
    cc = to_polymarket_exec_client_config(_cfg())
    assert cc.max_retries is None
    assert cc.retry_delay_initial_ms is None
    assert cc.retry_delay_max_ms is None


def test_polymarket_credentials_none_passthrough():
    cfg = _cfg()  # 全默认,凭证全 None
    cc = to_polymarket_data_client_config(cfg)
    assert cc.api_key is None
    assert cc.private_key is None


def test_orbitexch_data_client_config_maps_credentials():
    cfg = _cfg(venues={"orbitexch": {
        "username": "u", "password": "p", "headless": False, "browser_type": "firefox",
        "page_load_timeout_sec": 90.0,
    }})
    cc = to_orbitexch_data_client_config(cfg)
    assert cc.username == "u"
    assert cc.password == "p"
    assert cc.headless is False
    assert cc.browser_type == "firefox"
    assert cc.page_timeout == 90000


def test_orbitexch_data_client_config_maps_staleness():
    """#109:OE staleness_timeout(WS handler liveness timeout)经 dispatcher 进 OrbitExchDataClientConfig。
    旧 `health_interval_sec`(HealthCheckLoop tick)已随 #109 退役删除。"""
    cfg = _cfg(venues={"orbitexch": {"staleness_timeout_sec": 300}})
    cc = to_orbitexch_data_client_config(cfg)
    assert cc.staleness_timeout_secs == 300
    assert not hasattr(cc, "health_interval_secs")   # 已退役


def test_orbitexch_data_client_config_staleness_default():
    """未显式配置时:staleness 取 schema 默认(300)。"""
    cc = to_orbitexch_data_client_config(_cfg())
    assert cc.staleness_timeout_secs == 300


def test_orbitexch_credentials_empty_string_fallback():
    """OE Config 把 username/password 标为必填 str;env 缺失时 dispatcher 回退空串
    让下游 login 触发明确错误(loader 不预判)。"""
    cfg = _cfg()  # username/password 全 None
    cc = to_orbitexch_data_client_config(cfg)
    assert cc.username == ""
    assert cc.password == ""


def test_orbitexch_exec_client_config_maps_credentials():
    cfg = _cfg(venues={"orbitexch": {"username": "u", "password": "p", "page_load_timeout_sec": 90.0}})
    cc = to_orbitexch_exec_client_config(cfg)
    assert cc.username == "u"
    assert cc.password == "p"
    assert cc.page_timeout == 90000


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


def test_strategy_evaluator_config_log_evaluations_maps_strategy_section():
    cfg = _cfg(strategy={"log_evaluations": True})
    sc = to_strategy_evaluator_config(cfg)
    assert sc.log_evaluations is True


# ─── Risk / Context / Debug ───────────────────────────────────────────

def test_arb_risk_params_maps_fields():
    cfg = _cfg(risk={"match_tp": 0.08, "match_sl": -0.06, "global_sl": -0.20,
                     "min_probability": 0.04, "max_probability": 0.96})
    rp = to_arb_risk_params(cfg)
    assert rp.match_tp == 0.08
    assert rp.match_sl == -0.06
    assert rp.global_sl == -0.20
    assert rp.min_probability == 0.04
    assert rp.max_probability == 0.96


def test_arbitrage_params_maps_fields():
    cfg = _cfg(arbitrage={"share": 50.0, "max_leg_share": 75.0, "fx": 1.5})
    params = to_arbitrage_params(cfg)
    assert params.share == 50.0
    assert params.max_leg_share == 75.0
    assert params.fx == 1.5


def test_arb_context_init_kwargs_maps_execution_section():
    cfg = _cfg(execution={"tracking_timeout_sec": 45.0})
    kw = to_arb_context_init_kwargs(cfg)
    assert kw["pm_session_timeout_secs"] == 45.0
    assert kw["oe_session_timeout_secs"] == 45.0
    # #110/#109:PM/OE 健康 interval 均退役 —— 不再进 ArbContext。
    assert "pm_health_interval_secs" not in kw
    assert "oe_health_interval_secs" not in kw


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
