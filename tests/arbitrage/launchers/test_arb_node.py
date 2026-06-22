"""Slice 6:launcher 骨架(`launchers/arb_node.py`)。

不构造真 NT `TradingNode`(需 asyncio loop / 网络 / 凭证),用 `node_factory=MagicMock`
注入 mock 来验证:① 子函数纯映射;② orchestrator 调用顺序 + 各步参数。
"""

from unittest.mock import MagicMock
from unittest.mock import patch

import msgspec
import pytest
from py_clob_client_v2.exceptions import PolyApiException

import src.arbitrage.bootstrap as bootstrap
from launchers import arb_node
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.config.schema import ArbConfig
from src.arbitrage.debug.config import DebugConfig


def _cfg(**overrides) -> ArbConfig:
    return msgspec.convert(overrides, type=ArbConfig)


@pytest.fixture(autouse=True)
def _reset_ctx():
    bootstrap.reset_arb_context()
    yield
    bootstrap.reset_arb_context()


# ─── build_trading_node_config(纯映射)─────────────────────────

def test_build_trading_node_config_has_pm_oe_data_exec_clients():
    cfg = _cfg()
    nc = arb_node.build_trading_node_config(cfg)
    assert "POLYMARKET" in nc.data_clients
    assert "ORBITEXCH" in nc.data_clients
    assert "POLYMARKET" in nc.exec_clients
    assert "ORBITEXCH" in nc.exec_clients
    assert str(nc.trader_id) == "ARBITRAGE-001"
    assert nc.exec_engine.reconciliation is True
    assert nc.exec_engine.open_check_interval_secs == 300.0  # #111:连续 order 对账恢复 order liveness
    assert nc.exec_engine.position_check_interval_secs == 300.0  # #110:连续 position 对账驱动 PM merge/redeem + position liveness
    assert nc.exec_engine.inflight_check_interval_ms == 2_000
    assert nc.timeout_connection == 180.0


def test_build_trading_node_config_includes_credentials_from_cfg():
    cfg = _cfg(venues={"polymarket": {"clob_api_key": "K", "private_key": "0xpk"}})
    nc = arb_node.build_trading_node_config(cfg)
    assert nc.data_clients["POLYMARKET"].api_key == "K"
    assert nc.data_clients["POLYMARKET"].private_key == "0xpk"


# ─── prepare_runtime_state ────────────────────────────────────

def test_prepare_runtime_state_no_debug():
    cfg = _cfg()
    liveness, pair, inflight, dbg = arb_node.prepare_runtime_state(cfg)
    assert isinstance(liveness, VenueExecutionLiveness)
    assert isinstance(pair, PairRegistry)
    from src.arbitrage.common.pair_inflight import PairInFlightGate
    assert isinstance(inflight, PairInFlightGate)   # §6.10 §7:per-pair 闸进程级单例
    assert dbg is None


def test_prepare_runtime_state_enabled_debug():
    cfg = _cfg(debug={"enabled": True})
    liveness, pair, inflight, dbg = arb_node.prepare_runtime_state(cfg)
    assert isinstance(dbg, DebugConfig)
    assert dbg.enabled is True


def test_make_pm_settlement_skips_when_cleanup_disabled():
    cfg = _cfg(execution={"cleanup_enabled": False})

    assert arb_node._make_pm_settlement(cfg) is None


def test_make_pm_settlement_skips_without_chain_credentials():
    cfg = _cfg()

    assert arb_node._make_pm_settlement(cfg) is None


def test_make_pm_settlement_initializes_contract_and_flags(monkeypatch):
    cfg = _cfg(
        venues={
            "polymarket": {
                "private_key": "0xpk",
                "funder": "0xfunder",
                "builder_api_key": "BK",
                "builder_api_secret": "BS",
                "builder_passphrase": "BP",
                "relayer_url": "https://relayer.example/",
                "polygon_rpc_url": "https://polygon.example/",
            },
        },
        execution={"cleanup_merge_enabled": False, "cleanup_claim_enabled": True},
    )
    created = {}

    class _Contract:
        def __init__(self, config, logger=None):
            created["contract_config"] = config
            created["contract_logger"] = logger

        async def initialize(self):
            created["initialized"] = True
            return True

    class _Settlement:
        def __init__(self, contract, *, merge_enabled, claim_enabled, logger=None):
            created["settlement"] = self
            created["contract"] = contract
            created["merge_enabled"] = merge_enabled
            created["claim_enabled"] = claim_enabled
            created["settlement_logger"] = logger

    monkeypatch.setattr(arb_node, "PolymarketContractService", _Contract)
    monkeypatch.setattr(arb_node, "PolymarketSettlement", _Settlement)

    settlement = arb_node._make_pm_settlement(cfg)

    assert settlement is created["settlement"]
    assert created["initialized"] is True
    assert created["merge_enabled"] is False
    assert created["claim_enabled"] is True
    contract_config = created["contract_config"]
    assert contract_config.polymarket_private_key == "0xpk"
    assert contract_config.polymarket_funder == "0xfunder"
    assert contract_config.polymarket_builder_api_key == "BK"
    assert contract_config.polymarket_relayer_url == "https://relayer.example/"
    assert contract_config.polygon_rpc_url == "https://polygon.example/"


def test_make_pm_settlement_returns_none_when_contract_init_fails(monkeypatch):
    cfg = _cfg(venues={"polymarket": {"private_key": "0xpk", "funder": "0xfunder"}})

    class _Contract:
        def __init__(self, config, logger=None):
            pass

        async def initialize(self):
            return False

    settlement_ctor = MagicMock()
    monkeypatch.setattr(arb_node, "PolymarketContractService", _Contract)
    monkeypatch.setattr(arb_node, "PolymarketSettlement", settlement_ctor)

    assert arb_node._make_pm_settlement(cfg) is None
    settlement_ctor.assert_not_called()


# ─── register_factories(verifies 4 add_*_factory calls)────────

def test_register_factories_registers_pm_oe_data_exec():
    node = MagicMock()
    arb_node.register_factories(node)
    # #60:+ POLYMARKET-SPORTS data client factory(比分 firehose)
    assert node.add_data_client_factory.call_count == 3
    assert node.add_exec_client_factory.call_count == 2

    # venue 字符串校验
    data_venues = [call.args[0] for call in node.add_data_client_factory.call_args_list]
    exec_venues = [call.args[0] for call in node.add_exec_client_factory.call_args_list]
    assert set(data_venues) == {"POLYMARKET", "ORBITEXCH", "PMSPORTS"}
    assert set(exec_venues) == {"POLYMARKET", "ORBITEXCH"}


# ─── bootstrap_and_build orchestrator(call sequence)───────────

def test_bootstrap_and_build_full_call_sequence():
    """验证 install → TradingNode → prepare_context → factories → build → wire 顺序。

    `wire_arbitrage_runtime` 依赖 portfolio/risk_engine 是 ArbitragePortfolio/ArbitrageLiveRiskEngine
    isinstance 检查 — 用 spec MagicMock 兼容性差,直接 patch wire。
    """
    cfg = _cfg(risk={"share": 50.0, "fx": 1.5})
    fake_node = MagicMock()

    with patch.object(arb_node, "install_arbitrage_engines") as install_mock, \
         patch.object(arb_node, "wire_arbitrage_runtime") as wire_mock:
        node, liveness, pair = arb_node.bootstrap_and_build(cfg, node_factory=lambda config: fake_node)

    assert node is fake_node
    assert isinstance(liveness, VenueExecutionLiveness)
    assert isinstance(pair, PairRegistry)

    install_mock.assert_called_once()
    fake_node.add_data_client_factory.assert_called()
    fake_node.add_exec_client_factory.assert_called()
    fake_node.build.assert_called_once()
    wire_mock.assert_called_once()
    # wire_mock 收到 params 自 cfg.risk
    _, kwargs = wire_mock.call_args
    assert kwargs["params"].share == 50.0
    assert kwargs["params"].fx == 1.5
    assert kwargs["venue_liveness"] is liveness


def test_bootstrap_populates_arb_context():
    """`prepare_arb_context` 是 module-global 状态修改;验证 ArbContext 被填满。"""
    cfg = _cfg(execution={"tracking_timeout_sec": 45.0, "health_check_interval_sec": 90.0})
    fake_node = MagicMock()
    settlement = object()

    with patch.object(arb_node, "install_arbitrage_engines"), \
         patch.object(arb_node, "wire_arbitrage_runtime"), \
         patch.object(arb_node, "_make_pm_settlement", return_value=settlement):
        node, liveness, pair = arb_node.bootstrap_and_build(cfg, node_factory=lambda config: fake_node)

    ctx = bootstrap.get_arb_context()
    assert ctx.venue_liveness is liveness
    assert ctx.pair_registry is pair
    assert ctx.pm_session_timeout_secs == 45.0
    assert ctx.oe_session_timeout_secs == 45.0
    # #109/#110:PM/OE 健康 interval 死接线均已删(HealthCheckLoop 退役)
    assert not hasattr(ctx, "pm_health_interval_secs")
    assert not hasattr(ctx, "oe_health_interval_secs")
    assert not hasattr(ctx, "pm_positions_fetcher")
    assert ctx.pm_settlement is settlement


def test_bootstrap_install_invoked_with_debug_config_when_enabled():
    cfg = _cfg(debug={"enabled": True})
    fake_node = MagicMock()

    with patch.object(arb_node, "install_arbitrage_engines") as install_mock, \
         patch.object(arb_node, "wire_arbitrage_runtime"):
        arb_node.bootstrap_and_build(cfg, node_factory=lambda config: fake_node)

    _, kwargs = install_mock.call_args
    assert isinstance(kwargs["debug_config"], DebugConfig)
    assert kwargs["debug_config"].enabled is True


def test_bootstrap_install_invoked_with_none_when_no_debug():
    cfg = _cfg()
    fake_node = MagicMock()

    with patch.object(arb_node, "install_arbitrage_engines") as install_mock, \
         patch.object(arb_node, "wire_arbitrage_runtime"):
        arb_node.bootstrap_and_build(cfg, node_factory=lambda config: fake_node)

    _, kwargs = install_mock.call_args
    assert kwargs["debug_config"] is None


# ─── main CLI entry ───────────────────────────────────────────

def test_main_parses_config_and_runs(tmp_path, monkeypatch):
    cfg_path = tmp_path / "arb_config.json"
    cfg_path.write_text("{}")

    fake_node = MagicMock()
    monkeypatch.setattr(arb_node, "bootstrap_and_build",
                        lambda cfg, **kw: (fake_node, VenueExecutionLiveness(), PairRegistry()))

    rc = arb_node.main(["--config", str(cfg_path)])
    assert rc == 0
    fake_node.run.assert_called_once()
    fake_node.dispose.assert_called_once()


def test_main_preflight_polymarket_exits_before_build(tmp_path, monkeypatch):
    cfg_path = tmp_path / "arb_config.json"
    cfg_path.write_text("{}")

    preflight = MagicMock()
    build = MagicMock()
    monkeypatch.setattr(arb_node, "preflight_polymarket_trading", preflight)
    monkeypatch.setattr(arb_node, "bootstrap_and_build", build)

    rc = arb_node.main(["--config", str(cfg_path), "--preflight-polymarket"])

    assert rc == 0
    preflight.assert_called_once()
    build.assert_not_called()


def test_main_preflight_polymarket_failure_returns_2(tmp_path, monkeypatch, capsys):
    cfg_path = tmp_path / "arb_config.json"
    cfg_path.write_text("{}")

    def fail(_cfg):
        raise RuntimeError("Polymarket trading is geoblocked")

    monkeypatch.setattr(arb_node, "preflight_polymarket_trading", fail)
    monkeypatch.setattr(arb_node, "bootstrap_and_build", MagicMock())

    rc = arb_node.main(["--config", str(cfg_path), "--preflight-polymarket"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "Polymarket preflight failed" in err
    assert "geoblocked" in err


def test_main_preflight_polymarket_sdk_error_returns_2(tmp_path, monkeypatch, capsys):
    cfg_path = tmp_path / "arb_config.json"
    cfg_path.write_text("{}")

    def fail(_cfg):
        raise PolyApiException(error_msg="Request exception!")

    monkeypatch.setattr(arb_node, "preflight_polymarket_trading", fail)
    monkeypatch.setattr(arb_node, "bootstrap_and_build", MagicMock())

    rc = arb_node.main(["--config", str(cfg_path), "--preflight-polymarket"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "Polymarket preflight failed" in err
    assert "Request exception" in err


def test_preflight_polymarket_trading_uses_exec_proxy(monkeypatch, capsys):
    cfg = _cfg(venues={"polymarket": {"proxy_url": "http://127.0.0.1:7890"}})
    calls = []

    def fake_check(proxy_url):
        calls.append(proxy_url)
        return {"blocked": False, "country": "IE", "region": "", "ip": "203.0.113.42"}

    class _Client:
        def get_server_time(self):
            return 123

        def get_open_orders(self):
            return []

        def get_balance_allowance(self, params):
            return {"balance": "1000000"}

    monkeypatch.setattr(arb_node, "check_polymarket_geoblock", fake_check)
    monkeypatch.setattr(arb_node, "get_polymarket_http_client", lambda **kwargs: _Client())

    arb_node.preflight_polymarket_trading(cfg)

    assert calls == ["http://127.0.0.1:7890"]
    out = capsys.readouterr().out
    assert "Polymarket preflight OK" in out
    assert "open_order_count=0" in out
    assert "balance=1.000000 USDC.e" in out


def test_preflight_polymarket_trading_rejects_zero_balance(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr(
        arb_node,
        "check_polymarket_geoblock",
        lambda proxy_url: {"blocked": False, "country": "IE", "region": "", "ip": "203.0.113.42"},
    )

    class _Client:
        def get_server_time(self):
            return 123

        def get_open_orders(self):
            return []

        def get_balance_allowance(self, params):
            return {"balance": "0"}

    monkeypatch.setattr(arb_node, "get_polymarket_http_client", lambda **kwargs: _Client())

    with pytest.raises(RuntimeError, match="balance is zero"):
        arb_node.preflight_polymarket_trading(cfg)


def test_main_disposes_even_on_run_exception(tmp_path, monkeypatch):
    cfg_path = tmp_path / "arb_config.json"
    cfg_path.write_text("{}")

    fake_node = MagicMock()
    fake_node.run.side_effect = RuntimeError("boom")
    monkeypatch.setattr(arb_node, "bootstrap_and_build",
                        lambda cfg, **kw: (fake_node, VenueExecutionLiveness(), PairRegistry()))

    with pytest.raises(RuntimeError, match="boom"):
        arb_node.main(["--config", str(cfg_path)])
    fake_node.dispose.assert_called_once()


# ─── slice 8A:add_actors(必须 node.build 后调用)─────────────

def _add_actors_setup(monkeypatch, *, pm_provider=None, oe_provider=None):
    """共享 setup:重置 ArbContext,装 mock providers,monkeypatch asyncio。"""
    bootstrap.reset_arb_context()
    bootstrap.prepare_arb_context(
        pm_instrument_provider=pm_provider,
        oe_instrument_provider=oe_provider,
    )
    fake_loop = MagicMock(name="loop")
    import asyncio
    monkeypatch.setattr(asyncio, "get_event_loop", lambda: fake_loop)
    return fake_loop


def test_add_actors_2_total_matching_and_strategy(monkeypatch):
    """#58(slice A):InstrumentRefresher 退役(发现迁 DataClient 原生 _update_instruments)→
    add_actors 只装 MarketMatchingActor + StrategyEvaluator;provider 有无不再影响 actor 数。"""
    pm_prov, oe_prov = MagicMock(name="pm_prov"), MagicMock(name="oe_prov")
    _add_actors_setup(monkeypatch, pm_provider=pm_prov, oe_provider=oe_prov)
    node = MagicMock()
    node.kernel.portfolio = MagicMock(name="portfolio")

    arb_node.add_actors(node, _cfg(), pair_registry=PairRegistry())

    assert node.trader.add_actor.call_count == 2


def test_add_actors_skips_web_gateway_when_disabled(monkeypatch):
    """web.enabled=false(默认)→ 不构造 WebGatewayActor,仍只 2 个 actor。"""
    _add_actors_setup(monkeypatch)
    node = MagicMock()
    node.kernel.portfolio = MagicMock(name="portfolio")

    arb_node.add_actors(node, _cfg(), pair_registry=PairRegistry())

    assert node.trader.add_actor.call_count == 2


def test_add_actors_wires_web_gateway_when_enabled(monkeypatch):
    """web.enabled=true → 多装 WebGatewayActor(注入 kernel.portfolio)。"""
    from src.arbitrage.web.actor import WebGatewayActor

    _add_actors_setup(monkeypatch)
    node = MagicMock()
    portfolio_sentinel = MagicMock(name="portfolio_sentinel")
    node.kernel.portfolio = portfolio_sentinel

    arb_node.add_actors(node, _cfg(web={"enabled": True}), pair_registry=PairRegistry())

    assert node.trader.add_actor.call_count == 3
    web_actor = node.trader.add_actor.call_args_list[-1].args[0]
    assert isinstance(web_actor, WebGatewayActor)
    assert web_actor._portfolio is portfolio_sentinel


def test_add_actors_strategy_evaluator_receives_portfolio_from_kernel(monkeypatch):
    _add_actors_setup(monkeypatch)
    node = MagicMock()
    portfolio_sentinel = MagicMock(name="portfolio_sentinel")
    node.kernel.portfolio = portfolio_sentinel

    arb_node.add_actors(node, _cfg(), pair_registry=PairRegistry())

    # 最后一个 actor 是 StrategyEvaluator;它内部 _portfolio 是 kernel.portfolio
    last_call = node.trader.add_actor.call_args_list[-1]
    strategy_evaluator = last_call.args[0]
    assert strategy_evaluator._portfolio is portfolio_sentinel


def test_make_is_execution_active_false_when_no_session_in_flight(monkeypatch):
    """Q19 桥接:所有 exec client `_execution_active=False` → 聚合 callable 返 False。"""
    _add_actors_setup(monkeypatch)
    node = MagicMock()
    pm_client = MagicMock(); pm_client._execution_active = False
    oe_client = MagicMock(); oe_client._execution_active = False
    node.kernel.exec_engine._clients = {"PM": pm_client, "OE": oe_client}

    check = arb_node._make_is_execution_active(node)
    assert check() is False


def test_make_is_execution_active_true_when_any_session_in_flight(monkeypatch):
    """Q19:任一 client `_execution_active=True` → 聚合返 True(StrategyEvaluator 跳过 evaluate)。"""
    _add_actors_setup(monkeypatch)
    node = MagicMock()
    pm_client = MagicMock(); pm_client._execution_active = False
    oe_client = MagicMock(); oe_client._execution_active = True   # OE 有 session 在飞
    node.kernel.exec_engine._clients = {"PM": pm_client, "OE": oe_client}

    check = arb_node._make_is_execution_active(node)
    assert check() is True


def test_make_is_execution_active_tolerates_clients_without_property(monkeypatch):
    """非套利子类的 ExecClient(无 mixin)→ getattr 默认 False,不 raise。"""
    _add_actors_setup(monkeypatch)
    node = MagicMock()
    plain_client = object()    # 无 `_execution_active` 属性
    node.kernel.exec_engine._clients = {"PLAIN": plain_client}

    check = arb_node._make_is_execution_active(node)
    assert check() is False


def test_add_actors_wires_strategy_evaluator_with_real_is_execution_active(monkeypatch):
    """`add_actors` 装 StrategyEvaluator 时,`_is_execution_active` 是真聚合 callable,
    跟 exec client 的 `_execution_active` 联动(不是 `lambda: False`)。"""
    _add_actors_setup(monkeypatch)
    node = MagicMock()
    node.kernel.portfolio = MagicMock()
    pm_client = MagicMock(); pm_client._execution_active = True
    node.kernel.exec_engine._clients = {"PM": pm_client}

    arb_node.add_actors(node, _cfg(), pair_registry=PairRegistry())

    strategy_evaluator = node.trader.add_actor.call_args_list[-1].args[0]
    assert strategy_evaluator._is_execution_active() is True
    # 切换 client 状态后,callable 再调反映最新
    pm_client._execution_active = False
    assert strategy_evaluator._is_execution_active() is False


def test_bootstrap_and_build_invokes_add_actors(monkeypatch):
    """`bootstrap_and_build` 在 wire 后调 `add_actors`。"""
    cfg = _cfg()
    fake_node = MagicMock()
    add_actors_mock = MagicMock()

    with patch.object(arb_node, "install_arbitrage_engines"), \
         patch.object(arb_node, "wire_arbitrage_runtime"), \
         patch.object(arb_node, "add_actors", add_actors_mock):
        arb_node.bootstrap_and_build(cfg, node_factory=lambda config: fake_node)

    add_actors_mock.assert_called_once()
    _, kwargs = add_actors_mock.call_args
    assert "pair_registry" in kwargs


# ── 控制台 boot HALTED(#119)──────────────────────────────────────────
from nautilus_trader.model.enums import TradingState as _TradingState


def test_boot_halted_when_web_enabled_and_start_halted():
    fake_node = MagicMock()
    cfg = _cfg(web={"enabled": True, "start_halted": True})
    with patch.object(arb_node, "install_arbitrage_engines"), \
         patch.object(arb_node, "wire_arbitrage_runtime"), \
         patch.object(arb_node, "add_actors"):
        arb_node.bootstrap_and_build(cfg, node_factory=lambda config: fake_node)
    fake_node.kernel.risk_engine.set_trading_state.assert_called_once_with(_TradingState.HALTED)


def test_no_boot_halt_when_web_disabled():
    fake_node = MagicMock()
    cfg = _cfg()  # web 默认 disabled
    with patch.object(arb_node, "install_arbitrage_engines"), \
         patch.object(arb_node, "wire_arbitrage_runtime"), \
         patch.object(arb_node, "add_actors"):
        arb_node.bootstrap_and_build(cfg, node_factory=lambda config: fake_node)
    fake_node.kernel.risk_engine.set_trading_state.assert_not_called()
