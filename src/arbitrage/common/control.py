"""
Web 控制台 → 组件的运行时控制命令(方案乙:MessageBus 解耦)。

`WebGatewayActor` 是**单一生产者**,publish 这些命令到对应 topic;owner 组件订阅自行 apply
(risk 消费 trading_state/risk_params,matching 消费 refresh_interval)。契约真理源 = web §8.3。

写命令单向(web→组件),组件侧**校验 payload**:非法值拒绝并 log,不 apply(§8.5 真金边界)。
"""

from __future__ import annotations

from dataclasses import dataclass

# ── topic 常量 ────────────────────────────────────────────────────────
TOPIC_TRADING_STATE = "command.arb.trading_state"
TOPIC_RISK_PARAMS = "command.arb.risk_params"
TOPIC_ARBITRAGE_PARAMS = "command.arb.arbitrage_params"
TOPIC_REFRESH_INTERVAL = "command.arb.refresh_interval"


@dataclass(frozen=True)
class SetTradingStateCommand:
    """启停按钮 → NT `RiskEngine.set_trading_state`。state ∈ {"ACTIVE","HALTED"}(不用 REDUCING)。"""

    state: str


@dataclass(frozen=True)
class SetRiskParamsCommand:
    """热改风控参数;None 字段 = 不动(只覆盖给定项)。consumer=ArbitrageLiveRiskEngine。"""

    match_tp: float | None = None
    match_sl: float | None = None
    min_probability: float | None = None
    max_probability: float | None = None


@dataclass(frozen=True)
class SetArbitrageParamsCommand:
    """热改套利运行默认值;None 字段 = 不动。consumer=RiskEngine/StrategyEvaluator。"""

    share: float | None = None
    max_leg_share: float | None = None
    fx: float | None = None
    evaluate_on_depth_change: bool | None = None


@dataclass(frozen=True)
class SetRefreshIntervalCommand:
    """热改 matching/discovery 周期。consumer=MarketMatchingActor。"""

    secs: float
