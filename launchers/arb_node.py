"""
套利 NT 节点 launcher(slice 6,Q26)。

设计见 `docs/arbitrage/architectures/_cross-cutting/configuration.md §5.5`(launcher 骨架)。

启动顺序(refactor.md #41 + bootstrap.py 注释):
  1. `install_arbitrage_engines(debug_config=...)`  →  替换 kernel.Portfolio / .LiveRiskEngine
  2. `TradingNode(config)`                          →  kernel 原生构造 ArbitragePortfolio / ArbitrageLiveRiskEngine
  3. `prepare_arb_context(...)`                     →  填好 leg_settled / pair_registry / 间隔等
  4. `node.add_*_client_factory(...)` × 4           →  PM+OE × data+exec
  5. `node.build()`                                  →  factory.create 读 ArbContext 构造 client
  6. `wire_arbitrage_runtime(node, params=...)`     →  注入 ArbRiskParams 到已构造的 Portfolio/RiskEngine
  7. (slice 8 待补)Actors:InstrumentRefresher × 2 + MarketMatchingActor + StrategyEvaluator
  8. `node.run()` / `node.dispose()`

**slice 6 范围**:1-6 + node.run() / dispose。Actors / settlement / positions_fetcher 留 slice 8。
当前用空 StrategyRegistry(launcher 仍能起 connect / discovery / matching 链路,Q19 决策器 no-op evaluate)。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from py_clob_client_v2 import BalanceAllowanceParams
from py_clob_client_v2.clob_types import AssetType
from py_clob_client_v2.exceptions import PolyApiException

# 兜底:`python launchers/arb_node.py` 直跑时把项目根加 sys.path(`python -m launchers.arb_node` 不需要)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from nautilus_trader.adapters.orbitexch.data import ORBITEXCH
from nautilus_trader.adapters.orbitexch.factories import ArbOrbitExchLiveExecClientFactory
from nautilus_trader.adapters.orbitexch.factories import OrbitExchLiveDataClientFactory
from nautilus_trader.adapters.polymarket.arb_factories import ArbPolymarketLiveDataClientFactory
from nautilus_trader.adapters.polymarket.arb_factories import ArbPolymarketLiveExecClientFactory
from nautilus_trader.adapters.polymarket.arb_factories import PolymarketSportsLiveDataClientFactory
from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET
from nautilus_trader.adapters.polymarket.factories import get_polymarket_http_client
from nautilus_trader.adapters.polymarket.http.transport import check_polymarket_geoblock
from nautilus_trader.adapters.polymarket.sports import SPORTS_CLIENT
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId

from src.arbitrage.bootstrap import install_arbitrage_engines
from src.arbitrage.bootstrap import prepare_arb_context
from src.arbitrage.bootstrap import wire_arbitrage_runtime
from src.arbitrage.common.leg_settled import LegSettledRegistry
from src.arbitrage.common.pair_inflight import PairInFlightGate
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.config import ArbConfig
from src.arbitrage.config import load_arb_config
from src.arbitrage.config.dispatcher import to_arb_context_init_kwargs
from src.arbitrage.config.dispatcher import to_arb_risk_params
from src.arbitrage.config.dispatcher import to_debug_config
from src.arbitrage.config.dispatcher import to_market_matching_actor_config
from src.arbitrage.config.dispatcher import to_orbitexch_data_client_config
from src.arbitrage.config.dispatcher import to_orbitexch_exec_client_config
from src.arbitrage.config.dispatcher import to_polymarket_data_client_config
from src.arbitrage.config.dispatcher import to_polymarket_exec_client_config
from src.arbitrage.config.dispatcher import to_sports_data_client_config
from src.arbitrage.config.dispatcher import to_strategy_evaluator_config
from src.arbitrage.config.dispatcher import to_strategy_registry
from src.arbitrage.matching.actor import MarketMatchingActor
from src.arbitrage.matching.actor import _RuntimeDeps as MatchingDeps
from src.arbitrage.strategy.actor import StrategyEvaluator
from src.arbitrage.strategy.actor import _RuntimeDeps as StrategyDeps
from src.arbitrage.strategy.actions.place_bets import PlaceBetsAction
from src.arbitrage.strategy.check_action_registry import register_action
from src.arbitrage.strategy.check_action_registry import register_check
from src.arbitrage.strategy.checks.mean_rebate import MeanRebateCheck
from src.arbitrage.strategy.checks.mean_rebate_recovery import MeanRebateRecoveryCheck
from src.arbitrage.strategy.checks.pre_match import PreMatchCheck
from src.arbitrage.strategy.signals import SignalStore
from nautilus_trader.adapters.polymarket.common.conversion import usdce_from_units


def register_builtin_checks_and_actions() -> None:
    """slice 9(#49)用户域 Check / Action 注册。

    main() 顶部调一次;同名同类幂等(`register_check` / `register_action` 守门)。
    用户加自己的 Check / Action 类时,在 main 顶部追加同样的 register 调用即可。
    """
    register_check("pre_match", PreMatchCheck)
    register_check("mean_rebate", MeanRebateCheck)
    register_check("mean_rebate_recovery", MeanRebateRecoveryCheck)
    register_action("place_bets", PlaceBetsAction)


def build_trading_node_config(cfg: ArbConfig) -> TradingNodeConfig:
    """ArbConfig → NT `TradingNodeConfig`(PM+OE × data+exec 四 client config)。"""
    return TradingNodeConfig(
        trader_id=TraderId("ARBITRAGE-001"),
        logging=LoggingConfig(log_level="INFO"),
        exec_engine=LiveExecEngineConfig(reconciliation=False),
        data_clients={
            POLYMARKET: to_polymarket_data_client_config(cfg),
            ORBITEXCH: to_orbitexch_data_client_config(cfg),
            SPORTS_CLIENT: to_sports_data_client_config(cfg),   # #60:PM 比分 firehose
        },
        exec_clients={
            POLYMARKET: to_polymarket_exec_client_config(cfg),
            ORBITEXCH: to_orbitexch_exec_client_config(cfg),
        },
        timeout_connection=120.0,    # slice 7B(#53):PM event_slug_builder + load 100 events ~35s,需要>原 20s
        timeout_disconnection=10.0,
        timeout_post_stop=1.0,
    )


def prepare_runtime_state(cfg: ArbConfig):
    """共享 runtime 件:`(LegSettledRegistry, PairRegistry, PairInFlightGate, DebugConfig | None)`。

    LegSettledRegistry / PairRegistry / PairInFlightGate 是 launcher 持有的进程级单例,经
    ArbContext 传给 factory + Actor 装配(slice 8)。`PairInFlightGate`(§6.10 §7)被 strategy
    评估入口 + execution session 共享,做 per-pair 串行。
    """
    return LegSettledRegistry(), PairRegistry(), PairInFlightGate(), to_debug_config(cfg)


def register_factories(node: TradingNode) -> None:
    """注 4 个 factory(PM+OE × data+exec),NT `node.add_*_factory` 接口。"""
    node.add_data_client_factory(POLYMARKET, ArbPolymarketLiveDataClientFactory)
    node.add_data_client_factory(ORBITEXCH, OrbitExchLiveDataClientFactory)
    node.add_data_client_factory(SPORTS_CLIENT, PolymarketSportsLiveDataClientFactory)  # #60
    node.add_exec_client_factory(POLYMARKET, ArbPolymarketLiveExecClientFactory)
    node.add_exec_client_factory(ORBITEXCH, ArbOrbitExchLiveExecClientFactory)


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
    leg_settled: LegSettledRegistry | None = None,
) -> None:
    """slice 8A:**必须在 `node.build()` 之后调用**(provider 由 data factory 构造后回写到
    `ArbContext.{pm,oe}_instrument_provider`,Refresher 取同一实例确保 cache add 视图一致)。

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
                signal_collector=None,              # 用户域(slice 9 起)
                pair_inflight=pair_inflight,        # §6.10 §7:per-pair 串行(与 execution 共享同一份)
                pair_inflight_max_hold_secs=2 * cfg.execution.tracking_timeout_sec,  # 自愈上界 > 单笔套利最长耗时
                leg_settled=leg_settled,            # §6.10 §7:健检兜底 clear_all 的 arb 在飞判据
            ),
        ),
    )


def bootstrap_and_build(
    cfg: ArbConfig,
    *,
    node_factory=TradingNode,
) -> tuple[TradingNode, LegSettledRegistry, PairRegistry]:
    """主 orchestrator(slice 6:不接 Actors,留 slice 8)。

    `node_factory` 注入便于 test mock;生产路径走默认 `TradingNode`。
    返:`(node, leg_settled, pair_registry)` —— 后两个供 slice 8 Actors 装配复用同一对象。
    """
    leg_settled, pair_registry, pair_inflight, debug_config = prepare_runtime_state(cfg)

    # 1. 替换 kernel 类(必须在 TradingNode 之前)
    install_arbitrage_engines(debug_config=debug_config)

    # 2. 构造 node(kernel 原生构造 ArbitragePortfolio / ArbitrageLiveRiskEngine)
    node = node_factory(config=build_trading_node_config(cfg))

    # 3. 准备 ArbContext(必须在 node.build 之前;factory.create 读)
    #    `to_arb_context_init_kwargs(cfg)` 自带 oe_scraper_config / aliases(slice 7A)
    prepare_arb_context(
        leg_settled=leg_settled,
        pair_registry=pair_registry,
        pair_inflight=pair_inflight,    # §6.10 §7:strategy + execution 共享同一份 per-pair 闸
        debug_config=debug_config,
        pm_settlement=None,             # TODO slice 8/9:PolymarketSettlement 接线
        pm_positions_fetcher=None,      # TODO slice 8:positions_fetcher 接线
        **to_arb_context_init_kwargs(cfg),
    )

    # 4. 注 factories
    register_factories(node)

    # 5. build(factory.create 此时跑,读 ArbContext)
    node.build()

    # 6. wire 领域参数(必须在 node 构造后,Portfolio/RiskEngine 实例已存在)
    wire_arbitrage_runtime(
        node,
        params=to_arb_risk_params(cfg),
        leg_settled=leg_settled,
    )

    # 7. (slice 8A)接 4 个 Actor:provider 由 data factory 回写到 ArbContext 后可用
    add_actors(node, cfg, pair_registry=pair_registry, pair_inflight=pair_inflight, leg_settled=leg_settled)

    return node, leg_settled, pair_registry


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
    parser.add_argument("--config", required=True, help="path to arb_config.json")
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

    node, _, _ = bootstrap_and_build(cfg)

    try:
        node.run()
    finally:
        node.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
