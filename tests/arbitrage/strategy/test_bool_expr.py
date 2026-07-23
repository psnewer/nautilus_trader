"""BoolExpr —— 无状态 StateQuery 与 AND/OR/NOT 求值。"""

from src.arbitrage.strategy.bool_expr import AndExpr
from src.arbitrage.strategy.bool_expr import NotExpr
from src.arbitrage.strategy.bool_expr import OrExpr
from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.condition import EvalContext


class _ValueQuery(StateQuery):
    def __init__(self, key: str) -> None:
        self.key = key

    def matches(self, ctx: EvalContext) -> bool:
        return bool(ctx.strategy_defaults.get(self.key))


def _ctx(**values) -> EvalContext:
    return EvalContext(pair_id="match_X", strategy_defaults=values)


def test_state_query_reads_current_eval_context():
    query = _ValueQuery("live")
    assert query.eval(_ctx(live=True)) is True
    assert query.eval(_ctx(live=False)) is False


def test_and_expr_all_true():
    expr = AndExpr(_ValueQuery("a"), _ValueQuery("b"))
    assert expr.eval(_ctx(a=True, b=True)) is True
    assert expr.eval(_ctx(a=False, b=True)) is False


def test_or_expr_any_true():
    expr = OrExpr(_ValueQuery("a"), _ValueQuery("b"))
    assert expr.eval(_ctx(a=False, b=True)) is True
    assert expr.eval(_ctx(a=False, b=False)) is False


def test_not_expr_inverts():
    expr = NotExpr(_ValueQuery("a"))
    assert expr.eval(_ctx(a=True)) is False
    assert expr.eval(_ctx(a=False)) is True


def test_nested_and_or_not():
    expr = AndExpr(
        _ValueQuery("a"),
        OrExpr(_ValueQuery("b"), NotExpr(_ValueQuery("c"))),
    )
    assert expr.eval(_ctx(a=True, b=False, c=False)) is True
    assert expr.eval(_ctx(a=True, b=False, c=True)) is False
    assert expr.eval(_ctx(a=False, b=True, c=False)) is False


def test_empty_and_is_true_empty_or_is_false():
    ctx = _ctx()
    assert AndExpr().eval(ctx) is True
    assert OrExpr().eval(ctx) is False
