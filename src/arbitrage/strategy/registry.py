"""
Strategy / StrategyRegistry —— 按 scope 索引 + 挂载存在锁定(Q21-a)。

scope 优先级:**具体比赛(pair_id)> competition > sport**。
查找规则:**挂载存在就锁定本 scope**(即使本轮没命中也不下放到更低优先级 scope)—— 运营在
该 scope 挂 = 承诺该 scope 全权负责该 pair_id;留 None 即明示"该层无意见"。

每 scope **单策略**(Q21 收回"同 scope 并行多策略"):dict[scope] → 1 个 Strategy。
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from src.arbitrage.strategy.condition import Condition


@dataclass
class Strategy:
    """策略 = 套利树 + 补救树(各一棵 condition 嵌套树),evaluator 并行求值。"""

    scope_key: str                                  # "pair_id:X" / "comp:EPL" / "sport:Soccer";编码见 ScopeKey 约定
    arbitrage_tree: Condition
    compensation_tree: Condition
    metadata: dict = field(default_factory=dict)    # 可选:策略名、描述、参数


class StrategyRegistry:
    """三层 scope 索引:pair_id / competition / sport。**挂载存在锁定不降级**(Q21-a)。"""

    def __init__(self) -> None:
        self._by_pair: dict[str, Strategy] = {}
        self._by_competition: dict[str, Strategy] = {}
        self._by_sport: dict[str, Strategy] = {}

    # ── 挂载 ────────────────────────────────────────────────────────
    def register_pair(self, pair_id: str, strategy: Strategy) -> None:
        self._by_pair[pair_id] = strategy

    def register_competition(self, competition: str, strategy: Strategy) -> None:
        self._by_competition[competition] = strategy

    def register_sport(self, sport: str, strategy: Strategy) -> None:
        self._by_sport[sport] = strategy

    def unregister_pair(self, pair_id: str) -> None:
        self._by_pair.pop(pair_id, None)

    def unregister_competition(self, competition: str) -> None:
        self._by_competition.pop(competition, None)

    def unregister_sport(self, sport: str) -> None:
        self._by_sport.pop(sport, None)

    # ── 查询(Q21-a 挂载锁定)──────────────────────────────────────
    def get_for(
        self,
        pair_id: str | None,
        competition: str | None,
        sport: str | None,
    ) -> Strategy | None:
        """按优先级 pair_id > competition > sport,挂了就锁定本 scope,不降级。
        都没挂返 None(evaluator 端 no-op)。"""
        if pair_id is not None and pair_id in self._by_pair:
            return self._by_pair[pair_id]
        if competition is not None and competition in self._by_competition:
            return self._by_competition[competition]
        if sport is not None and sport in self._by_sport:
            return self._by_sport[sport]
        return None
