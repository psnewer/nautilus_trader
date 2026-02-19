"""
Mean Rebate 信号（平均返水）

计算跨平台套利的平均返水率。

计算逻辑：
1. 对于每个方向（home/draw/away），取两平台中概率最小的
2. 返水率 = 100 - min(P/O home) - min(P/O draw) - min(P/O away)
3. 如果返水率 > 阈值，返回 True

优点：
- 无论哪个结果发生，返水率都相同（平均分配）
- Size 计算简单：Polymarket = share, OrbitExch = share / odds

Size 计算（由执行服务处理）：
- discount = 0, take_off = 0（不加额外调整）
- Polymarket: size = share
- OrbitExch: size = share / odds
"""

from typing import Any

from .base import (
    Signal,
    SignalResult,
    MatchContext,
    ArbitrageDirection,
    ArbitrageLeg,
    ArbitrageVenue,
    ArbitrageAction,
)


class MeanRebateSignal(Signal):
    """
    平均返水信号

    计算各方向取最优价格后的总返水率。
    当返水率 > rate 参数时，信号为 True。
    """

    @property
    def name(self) -> str:
        return "mean_rebate"

    def calculate(self, context: MatchContext, params: dict[str, Any]) -> SignalResult:
        """
        计算平均返水率

        Args:
            context: 比赛上下文
            params: 信号参数
                - rate: 最小返水率阈值（默认 0.01，即 1%）

        Returns:
            计算结果
        """
        rate_threshold = params.get("rate", 0.01)

        poly_odds = context.polymarket_odds
        orbit_odds = context.orbitexch_odds

        # 转换赔率为概率
        poly_probs = self._convert_polymarket_odds(poly_odds)
        orbit_probs = self._convert_orbitexch_odds(orbit_odds)

        details = {
            "rate_threshold": rate_threshold,
            "polymarket_probabilities": poly_probs,
            "orbitexch_probabilities": orbit_probs,
        }

        # 检测市场类型
        poly_markets = set(poly_probs.keys())
        orbit_markets = set(orbit_probs.keys())
        available_markets = poly_markets & orbit_markets

        if not available_markets:
            return SignalResult(
                signal_name=self.name,
                satisfied=False,
                value=None,
                details={"error": "No matching markets", **details},
            )

        # 判断是 2-way 还是 3-way
        poly_has_draw = "draw" in poly_markets
        orbit_has_draw = "draw" in orbit_markets

        if poly_has_draw or orbit_has_draw:
            # 3-way：必须两边都有 draw
            if not (poly_has_draw and orbit_has_draw):
                return SignalResult(
                    signal_name=self.name,
                    satisfied=False,
                    value=None,
                    details={
                        "error": "3-way market incomplete",
                        "poly_has_draw": poly_has_draw,
                        "orbit_has_draw": orbit_has_draw,
                        **details,
                    },
                )
            outcomes = ["home", "draw", "away"]
        else:
            outcomes = ["home", "away"]

        # 检查所有必要的市场
        for outcome in outcomes:
            if outcome not in poly_probs or outcome not in orbit_probs:
                return SignalResult(
                    signal_name=self.name,
                    satisfied=False,
                    value=None,
                    details={"error": f"Missing {outcome} market", **details},
                )

        # 计算每个方向的最优概率
        best_probs = {}
        legs = []
        total_prob = 0.0

        for outcome in outcomes:
            poly_ask = poly_probs[outcome].get("ask", 0)
            orbit_ask = orbit_probs[outcome].get("ask", 0)

            if poly_ask <= 0 or orbit_ask <= 0:
                return SignalResult(
                    signal_name=self.name,
                    satisfied=False,
                    value=None,
                    details={"error": f"Invalid {outcome} ask price", **details},
                )

            # 选择最优价格
            if poly_ask <= orbit_ask:
                best_ask = poly_ask
                best_venue = ArbitrageVenue.POLYMARKET
                best_raw_odds = poly_ask / 100
            else:
                best_ask = orbit_ask
                best_venue = ArbitrageVenue.ORBITEXCH
                best_raw_odds = orbit_probs[outcome].get("raw_ask", 0)

            best_probs[outcome] = {
                "probability": best_ask,
                "venue": best_venue.value,
                "raw_odds": best_raw_odds,
            }
            total_prob += best_ask

            legs.append(
                ArbitrageLeg(
                    venue=best_venue,
                    market_type=outcome,
                    action=ArbitrageAction.BUY,
                    probability=best_ask,
                    raw_odds=best_raw_odds,
                )
            )

        # 计算平均返水率
        # 返水率 = (100 - total_prob) / 100（平均分配到所有方向）
        rebate_space = 100 - total_prob
        mean_rebate_rate = rebate_space / 100 if rebate_space > 0 else 0

        details["best_probs"] = best_probs
        details["total_probability"] = total_prob
        details["rebate_space"] = rebate_space
        details["mean_rebate_rate"] = mean_rebate_rate

        # 检查是否满足阈值
        satisfied = mean_rebate_rate >= rate_threshold

        if satisfied:
            # 创建套利方向（平均返水，不区分返水到哪个方向）
            direction = ArbitrageDirection(
                direction_id=f"{context.pair_id}_mean_rebate",
                legs=legs,
                total_probability=total_prob,
                rebate_rate=mean_rebate_rate,
                rebate_venue=ArbitrageVenue.POLYMARKET,  # 任意，平均返水无特定方向
                rebate_market="mean",  # 标记为平均返水
                description=f"Mean rebate across all outcomes",
            )
            context.add_direction(direction)
            details["direction"] = direction.to_dict()

        return SignalResult(
            signal_name=self.name,
            satisfied=satisfied,
            value=round(mean_rebate_rate, 4) if mean_rebate_rate else None,
            details=details,
        )

    def _convert_polymarket_odds(self, odds: dict) -> dict[str, dict[str, float]]:
        """转换 Polymarket 赔率为概率"""
        result = {}
        for market_type in ["home", "draw", "away"]:
            market_data = odds.get(market_type, {})
            if isinstance(market_data, dict):
                bid = market_data.get("bid", 0)
                ask = market_data.get("ask", 0)
                if bid > 0 or ask > 0:
                    result[market_type] = {
                        "bid": bid * 100 if bid > 0 else 0,
                        "ask": ask * 100 if ask > 0 else 0,
                    }
        return result

    def _convert_orbitexch_odds(self, odds: dict) -> dict[str, dict[str, float]]:
        """转换 OrbitExch 赔率为概率"""
        result = {}
        for market_type in ["home", "draw", "away"]:
            market_data = odds.get(market_type, {})
            if isinstance(market_data, dict):
                back = market_data.get("back", 0)
                lay = market_data.get("lay", 0)
                if back > 0 or lay > 0:
                    result[market_type] = {
                        "ask": 100 / back if back > 0 else 0,
                        "bid": 100 / lay if lay > 0 else 0,
                        "raw_ask": back,
                        "raw_bid": lay,
                    }
        return result
