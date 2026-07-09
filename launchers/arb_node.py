"""
套利 NT 节点 launcher(slice 6,Q26)。

设计见 `docs/arbitrage/architectures/_cross-cutting/configuration.md §5.5`(launcher 骨架)。

启动顺序(refactor.md #41 + bootstrap.py 注释):
  1. `install_arbitrage_engines(debug_config=...)`  →  替换 kernel.Portfolio / .LiveRiskEngine
  2. `TradingNode(config)`                          →  kernel 原生构造 ArbitragePortfolio / ArbitrageLiveRiskEngine
  3. `prepare_arb_context(...)`                     →  填好 venue_liveness / pair_registry / 间隔等
  4. `node.add_*_client_factory(...)`               →  enabled data sources + enabled venues
  5. `node.build()`                                  →  factory.create 读 ArbContext 构造 client
  6. `wire_arbitrage_runtime(node, params=...)`     →  注入 ArbRiskParams 到已构造的 Portfolio/RiskEngine
  7. Actors:MarketMatchingActor + StrategyEvaluator + optional WebGatewayActor
  8. `node.run()` / `node.dispose()`

当前 factory 注册由 Venue/DataSource Registry 驱动;PMSPORTS 是 data source,不是 trading venue。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from py_clob_client_v2 import BalanceAllowanceParams
from py_clob_client_v2.clob_types import AssetType
from py_clob_client_v2.exceptions import PolyApiException

# 兜底:`python launchers/arb_node.py` 直跑时把项目根加 sys.path(`python -m launchers.arb_node` 不需要)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from nautilus_trader.adapters.polymarket.factories import get_polymarket_http_client
from nautilus_trader.adapters.polymarket.contract import PolymarketContractService
from nautilus_trader.adapters.polymarket.http.transport import check_polymarket_geoblock
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.model.enums import TradingState
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId

from src.arbitrage.bootstrap import install_arbitrage_engines
from src.arbitrage.bootstrap import prepare_arb_context
from src.arbitrage.bootstrap import wire_arbitrage_runtime
from src.arbitrage.common.pair_inflight import PairInFlightGate
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.common.subscription_config import OddsSubscriptionConfig
from src.arbitrage.common.venues import enabled_data_sources
from src.arbitrage.common.venues import enabled_settlement_venues
from src.arbitrage.common.venues import enabled_sports_client_ids
from src.arbitrage.common.venues import enabled_venue_ids
from src.arbitrage.common.venues import enabled_venues
from src.arbitrage.common.venues import resolve_factory
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.config import ArbConfig
from src.arbitrage.config.schema import ConfigError
from src.arbitrage.config import load_arb_config
from src.arbitrage.config.dispatcher import to_arb_context_init_kwargs
from src.arbitrage.config.dispatcher import to_arb_risk_params
from src.arbitrage.config.dispatcher import to_arbitrage_params
from src.arbitrage.config.dispatcher import to_debug_config
from src.arbitrage.config.dispatcher import to_market_matching_actor_config
import src.arbitrage.config.dispatcher as config_dispatcher
from src.arbitrage.config.dispatcher import to_polymarket_data_client_config
from src.arbitrage.config.dispatcher import to_polymarket_exec_client_config
from src.arbitrage.config.dispatcher import to_strategy_evaluator_config
from src.arbitrage.config.dispatcher import to_strategy_registry
from src.arbitrage.config.dispatcher import to_web_gateway_config
from src.arbitrage.matching.actor import MarketMatchingActor
from src.arbitrage.matching.actor import _RuntimeDeps as MatchingDeps
from src.arbitrage.strategy.actor import StrategyEvaluator
from src.arbitrage.strategy.actor import _RuntimeDeps as StrategyDeps
from src.arbitrage.web.actor import WebGatewayActor
from src.arbitrage.web.actor import WebGatewayDeps
from src.arbitrage.strategy.actions.candi_select import CandiSelectAction
from src.arbitrage.strategy.actions.place_bets import PlaceBetsAction
from src.arbitrage.strategy.actions.share_limit import ShareLimitModification
from src.arbitrage.strategy.check_action_registry import register_action
from src.arbitrage.strategy.check_action_registry import register_check
from src.arbitrage.strategy.checks.cross_venue import RequireCrossVenueCheck
from src.arbitrage.strategy.checks.mean_rebate import MeanRebateCheck
from src.arbitrage.strategy.checks.mean_rebate_recovery import MeanRebateRecoveryCheck
from src.arbitrage.strategy.checks.one_side_rebate import OneSideRebateCheck
from src.arbitrage.strategy.signals import SignalStore
from nautilus_trader.adapters.polymarket.settlement import PolymarketSettlement
from nautilus_trader.adapters.polymarket.common.conversion import usdce_from_units


# 配置 Python logging 以输出 INFO 级别日志(settlement/contract service 使用标准 logging)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOG = logging.getLogger(__name__)


def register_builtin_checks_and_actions() -> None:
    """slice 9(#49)用户域 Check / Action 注册。

    main() 顶部调一次;同名同类幂等(`register_check` / `register_action` 守门)。
    用户加自己的 Check / Action 类时,在 main 顶部追加同样的 register 调用即可。
    """
    register_check("mean_rebate", MeanRebateCheck)
    register_check("mean_rebate_recovery", MeanRebateRecoveryCheck)
    register_check("one_side_rebate", OneSideRebateCheck)
    register_check("require_cross_venue", RequireCrossVenueCheck)
    register_action("share_limit", ShareLimitModification)
    register_action("candi_select", CandiSelectAction)
    register_action("place_bets", PlaceBetsAction)


def build_trading_node_config(cfg: ArbConfig) -> TradingNodeConfig:
    """ArbConfig → NT `TradingNodeConfig`(按 enabled data source / venue 生成 client config)。"""
    validate_venue_enablement(cfg)
    data_clients = {}
    exec_clients = {}
    for source in enabled_data_sources(cfg):
        data_clients[source.client_id] = getattr(config_dispatcher, source.data_config_builder)(cfg)
    for venue in enabled_venues(cfg):
        data_clients[venue.venue_id] = getattr(config_dispatcher, venue.data_config_builder)(cfg)
        if venue.exec_config_builder:
            exec_clients[venue.venue_id] = getattr(config_dispatcher, venue.exec_config_builder)(cfg)

    return TradingNodeConfig(
        trader_id=TraderId("ARBITRAGE-001"),
        logging=LoggingConfig(log_level="INFO"),
        exec_engine=LiveExecEngineConfig(
            reconciliation=True,
            # #111:开连续 open/order 对账(全局)—— 周期调 venue `generate_order_status_reports`:
            # PM 用于 order liveness 失败后的自动恢复;OE 健康时只读 CURRENT_BETS 内存,WS stale 时才 reload。
            open_check_interval_secs=cfg.execution.open_check_interval_secs,
            # #110:开连续 position 对账(全局)—— 周期调 venue `generate_position_status_reports`:
            # PM 据此跑 merge/redeem(fire-and-forget),OE 据此刷 venue_position_alive(WS 新鲜则不 reload)。
            position_check_interval_secs=cfg.execution.position_check_interval_secs,
        ),
        data_clients=data_clients,
        exec_clients=exec_clients,
        timeout_connection=180.0,    # #105:启动对账前需覆盖 OE 登录 + PM 初次 instrument load 最坏耗时
        timeout_disconnection=10.0,
        timeout_post_stop=1.0,
    )


def prepare_runtime_state(cfg: ArbConfig):
    """共享 runtime 件:`(VenueExecutionLiveness, PairRegistry, PairInFlightGate, DebugConfig | None)`。

    VenueExecutionLiveness / PairRegistry / PairInFlightGate 是 launcher 持有的进程级单例,经
    ArbContext 传给 factory + Actor 装配(slice 8)。`PairInFlightGate`(§6.10 §7)被 strategy
    评估入口 + execution session 共享,做 per-pair 串行。
    """
    validate_venue_enablement(cfg)
    return VenueExecutionLiveness(enabled_venue_ids(cfg)), PairRegistry(), PairInFlightGate(), to_debug_config(cfg)


def validate_venue_enablement(cfg: ArbConfig) -> None:
    """校验第一版 venue 插拔约束:至少两个 venue 开启,且存在 sports firehose anchor 数据源。"""
    enabled = enabled_runtime_venues(cfg)
    if len(enabled) < 2:
        raise ConfigError("At least two venues must be enabled under venues.*.enabled")
    if not enabled_sports_client_ids(cfg):
        raise ConfigError("sports_status data source must be enabled for PMSPORTS event anchor")


def enabled_runtime_venues(cfg: ArbConfig) -> tuple[str, ...]:
    """按配置返回会注册进 TradingNode runtime 的 venue 名称。"""
    return enabled_venue_ids(cfg)


def _make_pm_settlement(cfg: ArbConfig) -> PolymarketSettlement | None:
    """构造 PM merge/redeem 编排器。

    只在 cleanup 开启且链上操作所需基础凭证存在时初始化;初始化失败时返回 None,
    让交易节点继续启动,由后续 position reconcile 只做对账不做链上 settlement。
    """
    if not cfg.execution.cleanup_enabled:
        return None
    settlement_venues = enabled_settlement_venues(cfg, "polymarket_ctf")
    if not settlement_venues:
        return None

    pm = getattr(cfg.venues, settlement_venues[0].config_key)
    if not pm.private_key or not pm.funder:
        _LOG.warning("PM settlement disabled: missing POLYMARKET_PRIVATE_KEY or POLYMARKET_FUNDER")
        return None

    contract_config = OddsSubscriptionConfig(
        polymarket_private_key=pm.private_key or "",
        polymarket_funder=pm.funder or "",
        polymarket_builder_api_key=pm.builder_api_key or "",
        polymarket_builder_api_secret=pm.builder_api_secret or "",
        polymarket_builder_passphrase=pm.builder_passphrase or "",
        polymarket_relayer_url=pm.relayer_url,
        polygon_rpc_url=pm.polygon_rpc_url,
    )
    contract = PolymarketContractService(contract_config, logger=_LOG)
    if not asyncio.run(contract.initialize()):
        _LOG.warning("PM settlement disabled: PolymarketContractService initialize failed")
        return None

    return PolymarketSettlement(
        contract,
        merge_enabled=cfg.execution.cleanup_merge_enabled,
        claim_enabled=cfg.execution.cleanup_claim_enabled,
        logger=_LOG,
    )


def register_factories(node: TradingNode, cfg: ArbConfig | None = None) -> None:
    """注入 data source / venue factories;按 registry enablement 注册 runtime clients。"""
    if cfg is None:
        cfg = ArbConfig()
    validate_venue_enablement(cfg)
    for source in enabled_data_sources(cfg):
        node.add_data_client_factory(source.client_id, resolve_factory(source.data_factory))
    for venue in enabled_venues(cfg):
        if venue.data_factory is not None:
            node.add_data_client_factory(venue.venue_id, resolve_factory(venue.data_factory))
        if venue.exec_factory is not None:
            node.add_exec_client_factory(venue.venue_id, resolve_factory(venue.exec_factory))


def _make_is_execution_active(node: TradingNode):
    """Q19/§6.10 接线:聚合 PM+OE exec client 的 `_execution_active` property(`ArbExecutionSessionMixin`
    维护的 ref-count `len(_active_sessions) > 0`)。任一在飞 → True,StrategyEvaluator 跳过本轮 evaluate。

    跟健康检查共用同一 callable 语义(参 `_cross-cutting/synchronization.md` + `health_check.py:77`)。
    """
    exec_engine = node.kernel.exec_engine

    def check() -> bool:
        for client in exec_engine._clients.values():
            if getattr(client, "_execution_active", False):
                return True
        return False

    return check


def add_actors(
    node: TradingNode,
    cfg: ArbConfig,
    *,
    pair_registry: PairRegistry,
    pair_inflight: PairInFlightGate | None = None,
    config_path: str | None = None,
) -> None:
    """slice 8A:**必须在 `node.build()` 之后调用**。

    Data factories 已在 `node.build()` 期间构造 DataClient / Provider;周期发现由各
    DataClient 原生 `_update_instruments` 负责,launcher 这里只装配业务 Actor。

    构造 Actor + `node.trader.add_actor`:
      - `MarketMatchingActor`
      - `StrategyEvaluator`(`is_execution_active` Q19 桥到 exec client 的 `_execution_active`;
        `signal_collector=None` 留 slice 9 用户域)

    `loop` 用 `asyncio.get_event_loop()`;`SignalStore` 现地构造(每个 launcher 单实例)。

    #58(slice A):InstrumentRefresher 退役 —— 周期发现已迁进 PM/OE DataClient 原生
    `_update_instruments`(provider→`_handle_data`→DataEngine→cache + on_instrument)。
    matching 改自 timer 读 cache(不再依赖 InstrumentsRefreshed 事件)。
    """
    import asyncio

    # 防御:add_actors 在 node.run() 之前调,不保证有「当前」event loop(Py3.13 无当前 loop 时
    # get_event_loop() 抛 RuntimeError)。无则现地建一个并设为当前(actor 仅用它 create_task)。
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    # MarketMatchingActor:自 timer 读 cache → 算 MatchedPair → PairRegistry
    node.trader.add_actor(
        MarketMatchingActor(
            config=to_market_matching_actor_config(cfg),
            deps=MatchingDeps(pair_registry=pair_registry),
        ),
    )

    # StrategyEvaluator:消费 MatchedPair / OrderBookDeltas + 评估 Strategy 树
    node.trader.add_actor(
        StrategyEvaluator(
            config=to_strategy_evaluator_config(cfg),
            deps=StrategyDeps(
                pair_registry=pair_registry,
                strategy_registry=to_strategy_registry(cfg),
                portfolio=node.kernel.portfolio,
                signal_store=SignalStore(),
                is_execution_active=_make_is_execution_active(node),  # Q19:桥到 exec client `_execution_active`
                loop=loop,
                arbitrage_params=to_arbitrage_params(cfg),  # Web Arbitrage 运行时默认 share/max_leg_share/fx
                signal_collector=None,              # 用户域(slice 9 起)
                pair_inflight=pair_inflight,        # §6.10 §7:per-pair 串行(与 execution 共享同一份)
            ),
        ),
    )

    # WebGatewayActor(Step 7):只读监控网关。默认关闭 → enabled=false 时根本不构造/不占端口。
    if cfg.web.enabled:
        node.trader.add_actor(
            WebGatewayActor(
                config=to_web_gateway_config(cfg),
                deps=WebGatewayDeps(
                    loop=loop,
                    risk_engine=node.kernel.risk_engine,   # 读 trading_state / live risk params
                    arbitrage_params=to_arbitrage_params(cfg),  # 读/热改 Web Arbitrage 参数
                    config_path=config_path,                # PUT 写回 arb_config.json
                    pair_registry=pair_registry,            # /odds 遍历 matched pair 腿
                ),
            ),
        )


def bootstrap_and_build(
    cfg: ArbConfig,
    *,
    node_factory=TradingNode,
    config_path: str | None = None,
) -> tuple[TradingNode, VenueExecutionLiveness, PairRegistry]:
    """主 orchestrator(slice 6:不接 Actors,留 slice 8)。

    `node_factory` 注入便于 test mock;生产路径走默认 `TradingNode`。
    返:`(node, venue_liveness, pair_registry)` —— 后两个供 slice 8 Actors 装配复用同一对象。
    """
    venue_liveness, pair_registry, pair_inflight, debug_config = prepare_runtime_state(cfg)

    # 1. 替换 kernel 类(必须在 TradingNode 之前)
    install_arbitrage_engines(debug_config=debug_config)

    # 2. 构造 node(kernel 原生构造 ArbitragePortfolio / ArbitrageLiveRiskEngine)
    node = node_factory(config=build_trading_node_config(cfg))

    # 3. 准备 ArbContext(必须在 node.build 之前;factory.create 读)
    #    `to_arb_context_init_kwargs(cfg)` 只输出 venue/data-source keyed maps。
    prepare_arb_context(
        venue_liveness=venue_liveness,
        pair_registry=pair_registry,
        pair_inflight=pair_inflight,    # §6.10 §7:strategy + execution 共享同一份 per-pair 闸
        debug_config=debug_config,
        arbitrage_params=to_arbitrage_params(cfg),
        pm_settlement=_make_pm_settlement(cfg),  # #110:NT 连续 position 对账触发 merge/redeem
        **to_arb_context_init_kwargs(cfg),
    )

    # 4. 注 factories
    register_factories(node, cfg)

    # 5. build(factory.create 此时跑,读 ArbContext)
    node.build()

    # 6. wire 领域参数(必须在 node 构造后,Portfolio/RiskEngine 实例已存在)
    wire_arbitrage_runtime(
        node,
        params=to_arb_risk_params(cfg),
        arbitrage_params=to_arbitrage_params(cfg),
        venue_liveness=venue_liveness,
    )

    # 7. (slice 8A)接 4 个 Actor:provider 由 data factory 回写到 ArbContext 后可用
    add_actors(node, cfg, pair_registry=pair_registry, pair_inflight=pair_inflight, config_path=config_path)

    # 8. (#119)控制台启用时 boot 即 HALTED:真金安全默认,操作员经 web 点 Start 才放行。
    #    web 关闭则无按钮可解除 → 保持 NT 原生 ACTIVE,否则节点永不交易。
    if cfg.web.enabled and cfg.web.start_halted:
        node.kernel.risk_engine.set_trading_state(TradingState.HALTED)

    return node, venue_liveness, pair_registry


def preflight_polymarket_trading(cfg: ArbConfig) -> None:
    """Run a read-only PM trading route preflight without building the NT node."""
    pm_config = to_polymarket_exec_client_config(cfg)
    result = check_polymarket_geoblock(pm_config.proxy_url)
    client = get_polymarket_http_client(
        api_key=pm_config.api_key,
        api_secret=pm_config.api_secret,
        passphrase=pm_config.passphrase,
        base_url=pm_config.base_url_http,
        signature_type=pm_config.signature_type,
        private_key=pm_config.private_key,
        funder=pm_config.funder,
        proxy_url=pm_config.proxy_url,
    )
    server_time = client.get_server_time()
    open_orders = client.get_open_orders()
    balance_response = client.get_balance_allowance(
        BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=pm_config.signature_type,
        ),
    )
    balance = usdce_from_units(int(balance_response["balance"]))
    if balance.as_decimal() <= 0:
        raise RuntimeError(
            f"Polymarket CLOB balance is zero for signature_type={pm_config.signature_type}; "
            "check POLYMARKET_SIGNATURE_TYPE/funder before live trading",
        )
    country = result.get("country") or "unknown"
    region = result.get("region") or "unknown"
    ip_addr = result.get("ip") or "unknown"
    print(
        f"Polymarket preflight OK: country={country} region={region} ip={ip_addr} "
        f"server_time={server_time} open_order_count={len(open_orders or [])} "
        f"balance={balance}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Arbitrage NT live node launcher")
    parser.add_argument(
        "--config",
        default=str(_PROJECT_ROOT / "arb_config.json"),
        help="path to arb_config.json (default: project root arb_config.json)",
    )
    parser.add_argument(
        "--preflight-polymarket",
        action="store_true",
        help="run read-only Polymarket REST/geoblock preflight and exit",
    )
    args = parser.parse_args(argv)

    # 从项目根 .env 注入凭证(slice 10c smoke 发现:launcher 进程不自动 load,导致
    # 上游 PM `get_polymarket_api_key()` env fallback 路径触发 RuntimeError)
    try:
        from dotenv import load_dotenv
        load_dotenv(_PROJECT_ROOT / ".env")
    except ImportError:
        pass  # python-dotenv 未装时不强求(env 可经 shell 注入)

    register_builtin_checks_and_actions()    # slice 9:必须在 to_strategy_registry 之前(JSON 用 type 名查 registry)
    cfg = load_arb_config(args.config)
    if args.preflight_polymarket:
        try:
            preflight_polymarket_trading(cfg)
        except (RuntimeError, PolyApiException) as e:
            print(f"Polymarket preflight failed: {e}", file=sys.stderr)
            return 2
        return 0

    node, _, _ = bootstrap_and_build(cfg, config_path=args.config)

    try:
        node.run()
    finally:
        node.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
