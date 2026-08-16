"""Strategy 框架(Q21 锁定 2026-05-24):scope-priority + condition tree + 套利/补救并行。

详细设计:`docs/arbitrage/architectures/strategy/architecture.md`。
"""

from src.arbitrage.strategy.actor import StrategyEvaluator
from src.arbitrage.strategy.actor import StrategyEvaluatorConfig
from src.arbitrage.strategy.bool_expr import AndExpr
from src.arbitrage.strategy.bool_expr import BoolExpr
from src.arbitrage.strategy.bool_expr import NotExpr
from src.arbitrage.strategy.bool_expr import OrExpr
from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import AndCheckExpr
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import CheckExpr
from src.arbitrage.strategy.condition import Condition
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.condition import EvalResult
from src.arbitrage.strategy.condition import NotCheckExpr
from src.arbitrage.strategy.condition import OrCheckExpr
from src.arbitrage.strategy.condition import evaluate_tree
from src.arbitrage.strategy.registry import Strategy
from src.arbitrage.strategy.registry import StrategyRegistry
from src.arbitrage.strategy.runtime_store import StrategyRuntimeStore


__all__ = [
    "Action",
    "AndCheckExpr",
    "AndExpr",
    "BoolExpr",
    "Check",
    "CheckExpr",
    "Condition",
    "EvalContext",
    "EvalResult",
    "NotCheckExpr",
    "NotExpr",
    "OrCheckExpr",
    "OrExpr",
    "StateQuery",
    "Strategy",
    "StrategyEvaluator",
    "StrategyEvaluatorConfig",
    "StrategyRegistry",
    "StrategyRuntimeStore",
    "evaluate_tree",
]
