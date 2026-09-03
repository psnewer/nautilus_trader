"""Slice 4:ArbConfig → 各组件 config 的纯函数派发。

对应用例:config-dispatcher.{1-x}。
"""

import msgspec
import pytest

from src.arbitrage.config.dispatcher import _polymarket_ws_base_url
from src.arbitrage.config.dispatcher import to_arb_context_init_kwargs
from src.arbitrage.config.dispatcher import to_arb_risk_params
from src.arbitrage.config.dispatcher import to_arbitrage_params
from src.arbitrage.config.dispatcher import to_debug_config
from src.arbitrage.config.dispatcher import to_market_matching_actor_config
from src.arbitrage.config.dispatcher import to_orbitexch_data_client_config
from src.arbitrage.config.dispatcher import to_orbitexch_exec_client_config
from src.arbitrage.config.dispatcher import to_polymarket_data_client_config
from src.arbitrage.config.dispatcher import to_polymarket_exec_client_config
from src.arbitrage.config.dispatcher import to_se_discovery_config
from src.arbitrage.config.dispatcher import to_sharpexch_data_client_config
from src.arbitrage.config.dispatcher import to_sharpexch_exec_client_config
from src.arbitrage.config.dispatcher import to_sports_data_client_config
from src.arbitrage.config.dispatcher import to_strategy_evaluator_config
from src.arbitrage.config.schema import ArbConfig
from src.arbitrage.config.schema import ConfigError
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
    ],
)
def test_polymarket_ws_url_normalized_to_nt_base_url(raw, expected):
    assert _polymarket_ws_base_url(raw) == expected


@pytest.mark.parametrize("suffix", ["market", "user"])
def test_polymarket_ws_url_rejects_channel_endpoint(suffix):
    with pytest.raises(ConfigError, match="must be the base websocket URL"):
        _polymarket_ws_base_url(f"wss://ws-subscriptions-clob.polymarket.com/ws/{suffix}")


def test_polymarket_exec_client_config_maps_credentials():
    cfg = _cfg(venues={"polymarket": {
        "clob_api_key": "K",
        "ws_url": "wss://ws-subscriptions-clob.polymarket.com/ws/",
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


def test_browser_venue_proxy_maps_to_data_and_exec(  # #276:OE/SE 对称 proxy_url
):
    from src.arbitrage.config.dispatcher import to_orbitexch_exec_client_config
    from src.arbitrage.config.dispatcher import to_sharpexch_data_client_config
    from src.arbitrage.config.dispatcher import to_sharpexch_exec_client_config

    cfg = _cfg(venues={
        "orbitexch": {"proxy_url": "http://oe-proxy:7891"},
        "sharpexch": {"proxy_url": "http://se-proxy:7892"},
    })
    assert to_orbitexch_data_client_config(cfg).proxy_url == "http://oe-proxy:7891"
    assert to_orbitexch_exec_client_config(cfg).proxy_url == "http://oe-proxy:7891"
    assert to_sharpexch_data_client_config(cfg).proxy_url == "http://se-proxy:7892"
    assert to_sharpexch_exec_client_config(cfg).proxy_url == "http://se-proxy:7892"

    default_cfg = _cfg()
    assert to_orbitexch_data_client_config(default_cfg).proxy_url is None
    assert to_sharpexch_exec_client_config(default_cfg).proxy_url is None


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


def test_sports_data_client_config_maps_data_source_url_and_pm_proxy():
    cfg = _cfg(
        data_sources={"sports_status": {"ws_url": "wss://sports.example/ws"}},
        venues={"polymarket": {"proxy_url": "http://proxy.example:7890"}},
    )
    cc = to_sports_data_client_config(cfg)

    assert cc.sports_ws_url == "wss://sports.example/ws"
    assert cc.proxy_url == "http://proxy.example:7890"


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


def test_orbitexch_credentials_default_to_empty_string():
    """OE Config 把 username/password 标为必填 str;env 缺失时 dispatcher 转为空串
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


def test_sharpexch_data_client_config_maps_credentials():
    cfg = _cfg(venues={"sharpexch": {
        "username": "u",
        "password": "p",
        "base_url": "https://portal.example.com",
        "login_url": "https://login.example.com/player/",
        "headless": False,
        "browser_type": "firefox",
        "user_data_dir": "/tmp/se-playwright-profile",
        "page_load_timeout_sec": 90.0,
        "staleness_timeout_sec": 240,
    }})
    cc = to_sharpexch_data_client_config(cfg)
    assert cc.username == "u"
    assert cc.password == "p"
    assert cc.base_url == "https://portal.example.com"
    assert cc.login_url == "https://login.example.com/player/"
    assert cc.headless is False
    assert cc.browser_type == "firefox"
    assert cc.user_data_dir == "/tmp/se-playwright-profile"
    assert cc.page_timeout == 90000
    assert cc.staleness_timeout_secs == 240


def test_sharpexch_credentials_default_to_empty_string():
    cfg = _cfg()
    cc = to_sharpexch_data_client_config(cfg)
    assert cc.username == ""
    assert cc.password == ""


def test_sharpexch_exec_client_config_maps_credentials():
    cfg = _cfg(venues={"sharpexch": {
        "username": "u",
        "password": "p",
        "user_data_dir": "/tmp/se-playwright-profile",
        "page_load_timeout_sec": 90.0,
        "cloudflare_timeout_sec": 150.0,
    }})
    cc = to_sharpexch_exec_client_config(cfg)
    assert cc.username == "u"
    assert cc.password == "p"
    assert cc.user_data_dir == "/tmp/se-playwright-profile"
    assert cc.page_timeout == 90000
    assert cc.cloudflare_timeout == 150000


# ─── Actors ───────────────────────────────────────────────────────────

# #59(slice A):test_instrument_refresher_configs 删除 —— InstrumentRefresher 退役。


def test_market_matching_actor_config_maps_fields():
    cfg = _cfg(
        discovery={"refresh_interval_secs": 60.0},
        matching={"competition_max_matches": {"ATP": 1}},
    )
    mc = to_market_matching_actor_config(cfg)
    assert mc.refresh_interval_secs == 60.0
    assert mc.competition_max_matches == {"ATP": 1}
    assert mc.anchor_venue == "PMSPORTS"
    assert mc.tradable_venues == ("POLYMARKET", "ORBITEXCH")


def test_market_matching_actor_config_includes_sharpexch_when_enabled():
    cfg = _cfg(venues={"sharpexch": {"enabled": True}})
    mc = to_market_matching_actor_config(cfg)
    assert mc.tradable_venues == ("POLYMARKET", "ORBITEXCH", "SHARPEXCH")


def test_market_matching_actor_config_uses_enabled_tradable_venues():
    cfg = _cfg(venues={"orbitexch": {"enabled": False}, "sharpexch": {"enabled": True}})
    mc = to_market_matching_actor_config(cfg)
    assert mc.tradable_venues == ("POLYMARKET", "SHARPEXCH")


def test_market_matching_actor_config_supports_oe_se_only_with_pmsports_anchor():
    cfg = _cfg(venues={"polymarket": {"enabled": False}, "orbitexch": {"enabled": True}, "sharpexch": {"enabled": True}})
    mc = to_market_matching_actor_config(cfg)

    assert mc.anchor_venue == "PMSPORTS"
    assert mc.tradable_venues == ("ORBITEXCH", "SHARPEXCH")


def test_market_matching_empty_max_matches_becomes_none():
    """空 dict 不传入 MarketMatchingConfig(保持 None,由 actor 内部按空 map 处理)。"""
    cfg = _cfg()  # competition_max_matches 默认空 dict
    mc = to_market_matching_actor_config(cfg)
    assert mc.competition_max_matches is None


def test_strategy_evaluator_config_log_evaluations_default():
    cfg = _cfg()
    sc = to_strategy_evaluator_config(cfg)
    assert sc.log_evaluations is False
    assert str(sc.strategy_id) == "ARB-EVAL"
    assert sc.order_id_tag == "001"


def test_strategy_evaluator_config_log_evaluations_maps_strategy_section():
    cfg = _cfg(strategy={"log_evaluations": True})
    sc = to_strategy_evaluator_config(cfg)
    assert sc.log_evaluations is True


# ─── Risk / Context / Debug ───────────────────────────────────────────

def test_arb_risk_params_maps_fields():
    cfg = _cfg(risk={"match_tp": 0.08, "match_sl": -0.06,
                     "min_probability": 0.04, "max_probability": 0.96,
                     "prob_buy_only": True})
    rp = to_arb_risk_params(cfg)
    assert rp.match_tp == 0.08
    assert rp.match_sl == -0.06
    assert rp.min_probability == 0.04
    assert rp.max_probability == 0.96
    assert rp.prob_buy_only is True


def test_arbitrage_params_maps_fields():
    cfg = _cfg(arbitrage={
        "share": 50.0,
        "max_leg_share": 75.0,
        "fx": 1.5,
        "evaluate_on_depth_change": True,
    })
    params = to_arbitrage_params(cfg)
    assert params.share == 50.0
    assert params.max_leg_share == 75.0
    assert params.fx == 1.5
    assert params.evaluate_on_depth_change is True


def test_arb_context_init_kwargs_maps_execution_section():
    cfg = _cfg(execution={"tracking_timeout_sec": 45.0})
    kw = to_arb_context_init_kwargs(cfg)
    assert kw["session_timeout_secs_by_venue"] == {
        "POLYMARKET": 45.0,
        "ORBITEXCH": 45.0,
    }
    # #110/#109:PM/OE 健康 interval 均退役 —— 不再进 ArbContext。
    assert "pm_health_interval_secs" not in kw
    assert "oe_health_interval_secs" not in kw


def test_arb_context_keeps_pmsports_targets_when_polymarket_venue_disabled():
    cfg = _cfg(
        venues={"polymarket": {"enabled": False}, "orbitexch": {"enabled": True}, "sharpexch": {"enabled": True}},
        discovery={"polymarket": {"enabled": True, "sports": [{"sport": "Tennis", "competitions": ["atp"]}]}},
    )

    kw = to_arb_context_init_kwargs(cfg)

    assert kw["session_timeout_secs_by_venue"] == {
        "ORBITEXCH": 30.0,
        "SHARPEXCH": 30.0,
    }
    assert kw["target_competitions_by_data_source"] == {"PMSPORTS": ["atp"]}
    assert kw["competition_to_sport_by_data_source"] == {"PMSPORTS": {"atp": "Tennis"}}


def test_arb_context_prefers_explicit_sports_status_targets_over_default_polymarket_sports():
    cfg = _cfg(
        data_sources={"sports_status": {"sports": [{"sport": "Tennis", "competitions": ["wta"]}]}},
        discovery={"polymarket": {"enabled": True, "sports": [{"sport": "Soccer", "competitions": ["epl"]}]}},
    )

    kw = to_arb_context_init_kwargs(cfg)

    assert kw["target_competitions_by_data_source"] == {"PMSPORTS": ["wta"]}
    assert kw["competition_to_sport_by_data_source"] == {"PMSPORTS": {"wta": "Tennis"}}


def test_arb_context_pmsports_targets_default_to_polymarket_sports_when_data_sources_omitted():
    cfg = _cfg(
        discovery={"polymarket": {"enabled": True, "sports": [{"sport": "Soccer", "competitions": ["epl"]}]}},
    )

    kw = to_arb_context_init_kwargs(cfg)

    assert kw["target_competitions_by_data_source"] == {"PMSPORTS": ["epl"]}
    assert kw["competition_to_sport_by_data_source"] == {"PMSPORTS": {"epl": "Soccer"}}


def test_arb_context_omits_pmsports_targets_when_data_source_disabled():
    cfg = _cfg(
        data_sources={"sports_status": {"enabled": False}},
        discovery={"polymarket": {"enabled": True, "sports": [{"sport": "Tennis", "competitions": ["atp"]}]}},
    )

    kw = to_arb_context_init_kwargs(cfg)

    assert kw["target_competitions_by_data_source"] == {"PMSPORTS": []}
    assert kw["competition_to_sport_by_data_source"] == {"PMSPORTS": {}}


# ── slice 7A:OE scraper config + aliases 进 ArbContext ────

def test_arb_context_init_kwargs_includes_oe_scraper_config():
    cfg = _cfg(
        discovery={"orbitexch": {"enabled": True,
                                  "sports": [{"sport": "Tennis", "competitions": ["Men's Roland Garros 2026"]}]}},
        venues={"orbitexch": {"headless": False, "page_load_timeout_sec": 90.0}},
    )
    kw = to_arb_context_init_kwargs(cfg)
    sc = kw["discovery_config_by_venue"]["ORBITEXCH"]
    assert sc is not None
    assert sc.enabled is True
    # #62:scraper 强制 headless + 无登录 profile(免登录、后台定时、与 data/exec 登录浏览器解耦),
    # 不跟随 venue.headless(=False)
    assert sc.browser.headless is True
    assert sc.browser.user_data_dir is None
    assert sc.browser.timeout_ms == 90000   # 90s → 90000ms
    assert sc.sports[0].sport == "Tennis"
    assert sc.sports[0].competitions == ["Men's Roland Garros 2026"]
    assert kw["discovery_config_by_venue"]["ORBITEXCH"] is sc
    assert "POLYMARKET" not in kw["discovery_config_by_venue"]
    assert kw["sport_aliases_by_venue"]["ORBITEXCH"] == dict(cfg.matching.sport_aliases)
    assert kw["competition_aliases_by_venue"]["ORBITEXCH"] == dict(cfg.matching.competition_aliases)


def test_arb_context_init_kwargs_oe_scraper_config_none_when_disabled():
    cfg = _cfg(discovery={"orbitexch": {"enabled": False}})
    kw = to_arb_context_init_kwargs(cfg)
    assert "ORBITEXCH" not in kw["discovery_config_by_venue"]


def test_arb_context_init_kwargs_omits_oe_scraper_when_venue_disabled():
    cfg = _cfg(
        venues={
            "polymarket": {"enabled": True},
            "orbitexch": {"enabled": False},
            "sharpexch": {"enabled": True},
        },
        discovery={"orbitexch": {"enabled": True,
                                  "sports": [{"sport": "Tennis", "competitions": ["Wimbledon"]}]}},
    )
    kw = to_arb_context_init_kwargs(cfg)
    assert "ORBITEXCH" not in kw["discovery_config_by_venue"]


def test_se_discovery_config_maps_fields():
    cfg = _cfg(
        discovery={"sharpexch": {"enabled": True,
                                  "sports": [{"sport": "Tennis", "competitions": ["Men's Wimbledon 2026"]}]}},
        venues={"sharpexch": {"enabled": True, "headless": False, "page_load_timeout_sec": 90.0}},
    )
    sc = to_se_discovery_config(cfg)
    assert sc is not None
    assert sc.enabled is True
    assert sc.browser.headless is True
    assert sc.browser.user_data_dir is None
    assert sc.browser.timeout_ms == 90000
    assert sc.sports[0].sport == "Tennis"
    assert sc.sports[0].competitions == ["Men's Wimbledon 2026"]


def test_se_discovery_config_none_when_disabled():
    cfg = _cfg(venues={"sharpexch": {"enabled": True}}, discovery={"sharpexch": {"enabled": False}})
    assert to_se_discovery_config(cfg) is None


def test_se_discovery_config_none_when_venue_disabled():
    cfg = _cfg(
        venues={"sharpexch": {"enabled": False}},
        discovery={"sharpexch": {"enabled": True,
                                  "sports": [{"sport": "Tennis", "competitions": ["Wimbledon"]}]}},
    )
    assert to_se_discovery_config(cfg) is None


def test_arb_context_init_kwargs_omits_se_discovery_when_venue_disabled():
    cfg = _cfg(
        venues={"sharpexch": {"enabled": False}},
        discovery={"sharpexch": {"enabled": True,
                                  "sports": [{"sport": "Tennis", "competitions": ["Wimbledon"]}]}},
    )
    kw = to_arb_context_init_kwargs(cfg)
    assert "SHARPEXCH" not in kw["discovery_config_by_venue"]


def test_arb_context_init_kwargs_includes_se_discovery_when_venue_enabled():
    cfg = _cfg(
        venues={"sharpexch": {"enabled": True}},
        discovery={"sharpexch": {"enabled": True,
                                  "sports": [{"sport": "Tennis", "competitions": ["Wimbledon"]}]}},
        matching={"sport_aliases": {"tennis": "Tennis"}, "competition_aliases": {"w": "Wimbledon"}},
    )
    kw = to_arb_context_init_kwargs(cfg)
    assert kw["discovery_config_by_venue"]["SHARPEXCH"].sports[0].competitions == ["Wimbledon"]
    assert kw["sport_aliases_by_venue"]["SHARPEXCH"] == {"tennis": "Tennis"}
    assert kw["competition_aliases_by_venue"]["SHARPEXCH"] == {"w": "Wimbledon"}
    assert kw["session_timeout_secs_by_venue"] == {
        "POLYMARKET": 30.0,
        "ORBITEXCH": 30.0,
        "SHARPEXCH": 30.0,
    }


def test_arb_context_init_kwargs_includes_aliases():
    cfg = _cfg(matching={"sport_aliases": {"soccer": "Soccer"},
                         "competition_aliases": {"atp": "ATP",
                                                  "Men's Roland Garros 2026": "ATP"}})
    kw = to_arb_context_init_kwargs(cfg)
    assert kw["sport_aliases_by_venue"] == {
        "POLYMARKET": {"soccer": "Soccer"},
        "ORBITEXCH": {"soccer": "Soccer"},
    }
    assert kw["competition_aliases_by_venue"]["POLYMARKET"]["atp"] == "ATP"
    assert kw["competition_aliases_by_venue"]["ORBITEXCH"]["Men's Roland Garros 2026"] == "ATP"


# ── PMSPORTS 发现目标 + competition→sport map ──────────────────

def test_sports_status_target_competitions_from_polymarket_sports():
    cfg = _cfg(discovery={"polymarket": {"enabled": True,
                                          "sports": [{"sport": "Tennis", "competitions": ["atp", "wta"]}]}})
    kw = to_arb_context_init_kwargs(cfg)
    assert kw["target_competitions_by_data_source"] == {"PMSPORTS": ["atp", "wta"]}


def test_sports_status_target_competitions_empty_when_polymarket_discovery_disabled():
    cfg = _cfg(discovery={"polymarket": {"enabled": False}})
    kw = to_arb_context_init_kwargs(cfg)
    assert kw["target_competitions_by_data_source"] == {"PMSPORTS": []}


def test_sports_status_target_competitions_survive_when_polymarket_venue_disabled():
    cfg = _cfg(
        venues={
            "polymarket": {"enabled": False},
            "orbitexch": {"enabled": True},
            "sharpexch": {"enabled": True},
        },
        discovery={"polymarket": {"enabled": True,
                                   "sports": [{"sport": "Tennis", "competitions": ["atp"]}]}},
    )
    kw = to_arb_context_init_kwargs(cfg)
    assert kw["target_competitions_by_data_source"] == {"PMSPORTS": ["atp"]}
    assert kw["competition_to_sport_by_data_source"] == {"PMSPORTS": {"atp": "Tennis"}}


def test_sports_status_competition_to_sport_map():
    cfg = _cfg(discovery={"polymarket": {"enabled": True,
                                          "sports": [{"sport": "Tennis", "competitions": ["atp", "wta"]},
                                                     {"sport": "Soccer", "competitions": ["epl"]}]}})
    kw = to_arb_context_init_kwargs(cfg)
    assert kw["competition_to_sport_by_data_source"] == {
        "PMSPORTS": {"atp": "Tennis", "wta": "Tennis", "epl": "Soccer"},
    }


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
