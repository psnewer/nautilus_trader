"""
返水率信号 (Rebate Signal)

计算双边市场的套利返水率。

套利计算逻辑：
1. 转换赔率为概率：
   - OrbitExch: probability = 100 / decimal_odds
     - back (支持价) = ask (买入价)
     - lay (反对价) = bid (卖出价)
   - Polymarket: probability = value * 100
     - bid = 卖出价
     - ask = 买入价

2. 计算互斥概率和（不同平台的 ask 相加）：
   - 2-outcome: OrbitExch home ask + Polymarket away ask
   - 3-outcome: 覆盖所有结果的组合

3. 如果概率和 < 100，存在套利空间：
   - rebate_rate = (100 - sum) / stake_probability
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


class RebateSignal(Signal):
    """
    返水率信号

    计算 Polymarket 和 OrbitExch 之间的套利返水率。
    当返水率 > rate 参数时，信号为 True，并将套利方向写入 context.arbitrage_directions。
    """

    @property
    def name(self) -> str:
        return "rebate"

    def calculate(self, context: MatchContext, params: dict[str, Any]) -> SignalResult:
        """
        计算返水率信号

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

        # 转换赔率为概率（统一使用 ask/bid 术语）
        poly_probs = self._convert_polymarket_odds(poly_odds)
        orbit_probs = self._convert_orbitexch_odds(orbit_odds)

        details = {
            "rate_threshold": rate_threshold,
            "polymarket_probabilities": poly_probs,
            "orbitexch_probabilities": orbit_probs,
            "directions": [],
        }

        # 检测市场类型（2-outcome 或 3-outcome）
        available_markets = set(poly_probs.keys()) & set(orbit_probs.keys())

        if not available_markets:
            return SignalResult(
                signal_name=self.name,
                satisfied=False,
                value=None,
                details={"error": "No matching markets", **details},
            )

        has_draw = "draw" in available_markets

        # 计算套利方向
        if has_draw:
            self._calculate_3way_arbitrage(
                context, poly_probs, orbit_probs, rate_threshold, details
            )
        else:
            self._calculate_2way_arbitrage(
                context, poly_probs, orbit_probs, rate_threshold, details
            )

        # 获取最佳方向
        best_direction = context.get_best_direction()
        max_rebate = best_direction.rebate_rate if best_direction else 0.0
        satisfied = max_rebate >= rate_threshold

        return SignalResult(
            signal_name=self.name,
            satisfied=satisfied,
            value=round(max_rebate, 4) if best_direction else None,
            details=details,
        )

    def _convert_polymarket_odds(self, odds: dict) -> dict[str, dict[str, float]]:
        """
        转换 Polymarket 赔率为概率

        Polymarket 赔率已经是概率形式 (0-1)，乘以 100 转换为百分比。
        - bid = 卖出价（你能卖出的价格）
        - ask = 买入价（你需要付出的价格）

        Returns:
            {market_type: {"bid": prob, "ask": prob}}
        """
        result = {}
        for market_type in ["home", "draw", "away"]:
            market_data = odds.get(market_type, {})
            if isinstance(market_data, dict):
                bid = market_data.get("bid", 0)
                ask = market_data.get("ask", 0)
                if bid > 0 or ask > 0:
                    result[market_type] = {
                        "bid": bid * 100 if bid > 0 else 0,  # 卖出价
                        "ask": ask * 100 if ask > 0 else 0,  # 买入价
                    }
        return result

    def _convert_orbitexch_odds(self, odds: dict) -> dict[str, dict[str, float]]:
        """
        转换 OrbitExch 赔率为概率

        OrbitExch 赔率是十进制赔率，概率 = 100 / odds。
        - back (支持价) = ask (买入价) - 你买入这个结果的价格
        - lay (反对价) = bid (卖出价) - 你卖出这个结果的价格

        Returns:
            {market_type: {"ask": prob, "bid": prob, "raw_ask": odds, "raw_bid": odds}}
        """
        result = {}
        for market_type in ["home", "draw", "away"]:
            market_data = odds.get(market_type, {})
            if isinstance(market_data, dict):
                back = market_data.get("back", 0)  # back = ask
                lay = market_data.get("lay", 0)    # lay = bid
                if back > 0 or lay > 0:
                    result[market_type] = {
                        "ask": 100 / back if back > 0 else 0,  # back -> ask (买入价)
                        "bid": 100 / lay if lay > 0 else 0,    # lay -> bid (卖出价)
                        "raw_ask": back,  # 原始 back 赔率
                        "raw_bid": lay,   # 原始 lay 赔率
                    }
        return result

    def _calculate_2way_arbitrage(
        self,
        context: MatchContext,
        poly_probs: dict,
        orbit_probs: dict,
        rate_threshold: float,
        details: dict,
    ) -> None:
        """
        计算 2-outcome 市场的套利方向

        套利组合（使用 ask 价格买入互斥结果）：
        1. OrbitExch home ask + Polymarket away ask
        2. OrbitExch away ask + Polymarket home ask
        """
        directions_info = []

        # 检查必要数据
        if "home" not in poly_probs or "away" not in poly_probs:
            return
        if "home" not in orbit_probs or "away" not in orbit_probs:
            return

        # 方向 1: OrbitExch buy home (ask) + Polymarket buy away (ask)
        # 如果 home 赢，OrbitExch 赚；如果 away 赢，Polymarket 赚
        orbit_home_ask = orbit_probs["home"].get("ask", 0)
        poly_away_ask = poly_probs["away"].get("ask", 0)

        if orbit_home_ask > 0 and poly_away_ask > 0:
            total_prob_1 = orbit_home_ask + poly_away_ask
            if total_prob_1 < 100:
                rebate_rate_1 = (100 - total_prob_1) / poly_away_ask
                direction_1 = ArbitrageDirection(
                    direction_id=f"{context.pair_id}_orbit_home_poly_away",
                    legs=[
                        ArbitrageLeg(
                            venue=ArbitrageVenue.ORBITEXCH,
                            market_type="home",
                            action=ArbitrageAction.BUY,
                            probability=orbit_home_ask,
                            raw_odds=orbit_probs["home"].get("raw_ask", 0),
                        ),
                        ArbitrageLeg(
                            venue=ArbitrageVenue.POLYMARKET,
                            market_type="away",
                            action=ArbitrageAction.BUY,
                            probability=poly_away_ask,
                            raw_odds=poly_away_ask / 100,
                        ),
                    ],
                    total_probability=total_prob_1,
                    rebate_rate=rebate_rate_1,
                    rebate_venue=ArbitrageVenue.POLYMARKET,
                    rebate_market="away",
                    description=f"Buy OrbitExch home + Polymarket away",
                )
                context.add_direction(direction_1)
                directions_info.append(direction_1.to_dict())

        # 方向 2: OrbitExch buy away (ask) + Polymarket buy home (ask)
        orbit_away_ask = orbit_probs["away"].get("ask", 0)
        poly_home_ask = poly_probs["home"].get("ask", 0)

        if orbit_away_ask > 0 and poly_home_ask > 0:
            total_prob_2 = orbit_away_ask + poly_home_ask
            if total_prob_2 < 100:
                rebate_rate_2 = (100 - total_prob_2) / poly_home_ask
                direction_2 = ArbitrageDirection(
                    direction_id=f"{context.pair_id}_orbit_away_poly_home",
                    legs=[
                        ArbitrageLeg(
                            venue=ArbitrageVenue.ORBITEXCH,
                            market_type="away",
                            action=ArbitrageAction.BUY,
                            probability=orbit_away_ask,
                            raw_odds=orbit_probs["away"].get("raw_ask", 0),
                        ),
                        ArbitrageLeg(
                            venue=ArbitrageVenue.POLYMARKET,
                            market_type="home",
                            action=ArbitrageAction.BUY,
                            probability=poly_home_ask,
                            raw_odds=poly_home_ask / 100,
                        ),
                    ],
                    total_probability=total_prob_2,
                    rebate_rate=rebate_rate_2,
                    rebate_venue=ArbitrageVenue.POLYMARKET,
                    rebate_market="home",
                    description=f"Buy OrbitExch away + Polymarket home",
                )
                context.add_direction(direction_2)
                directions_info.append(direction_2.to_dict())

        details["directions"] = directions_info

    def _calculate_3way_arbitrage(
        self,
        context: MatchContext,
        poly_probs: dict,
        orbit_probs: dict,
        rate_threshold: float,
        details: dict,
    ) -> None:
        """
        计算 3-outcome 市场的套利方向

        每个组合必须覆盖所有三个结果 (home, draw, away)。
        组合策略：从一个平台选一个结果的 ask，从另一个平台选另外两个结果的 ask。
        """
        directions_info = []
        outcomes = ["home", "draw", "away"]

        # 检查必要数据
        for outcome in outcomes:
            if outcome not in poly_probs or outcome not in orbit_probs:
                return

        # 策略：OrbitExch 买一个 (ask)，Polymarket 买其余两个 (ask)
        for orbit_outcome in outcomes:
            other_outcomes = [o for o in outcomes if o != orbit_outcome]

            # OrbitExch buy outcome (ask) + Polymarket buy other two (ask)
            orbit_ask = orbit_probs[orbit_outcome].get("ask", 0)
            if orbit_ask <= 0:
                continue

            poly_asks_sum = 0
            poly_legs = []
            valid = True

            for poly_outcome in other_outcomes:
                poly_ask = poly_probs[poly_outcome].get("ask", 0)
                if poly_ask <= 0:
                    valid = False
                    break
                poly_asks_sum += poly_ask
                poly_legs.append(
                    ArbitrageLeg(
                        venue=ArbitrageVenue.POLYMARKET,
                        market_type=poly_outcome,
                        action=ArbitrageAction.BUY,
                        probability=poly_ask,
                        raw_odds=poly_ask / 100,
                    )
                )

            if not valid:
                continue

            total_prob = orbit_ask + poly_asks_sum
            if total_prob < 100:
                # 返水率计算：按 Polymarket 投入比例
                rebate_rate = (100 - total_prob) / poly_asks_sum

                # 确定主要返水市场（概率较高的 Polymarket 市场）
                main_poly_market = max(
                    other_outcomes,
                    key=lambda o: poly_probs[o].get("ask", 0)
                )

                direction = ArbitrageDirection(
                    direction_id=f"{context.pair_id}_orbit_{orbit_outcome}_poly_others",
                    legs=[
                        ArbitrageLeg(
                            venue=ArbitrageVenue.ORBITEXCH,
                            market_type=orbit_outcome,
                            action=ArbitrageAction.BUY,
                            probability=orbit_ask,
                            raw_odds=orbit_probs[orbit_outcome].get("raw_ask", 0),
                        ),
                        *poly_legs,
                    ],
                    total_probability=total_prob,
                    rebate_rate=rebate_rate,
                    rebate_venue=ArbitrageVenue.POLYMARKET,
                    rebate_market=main_poly_market,
                    description=f"Buy OrbitExch {orbit_outcome} + Polymarket {'+'.join(other_outcomes)}",
                )
                context.add_direction(direction)
                directions_info.append(direction.to_dict())

        # 策略：Polymarket 买一个 (ask)，OrbitExch 买其余两个 (ask)
        for poly_outcome in outcomes:
            other_outcomes = [o for o in outcomes if o != poly_outcome]

            # Polymarket buy outcome (ask) + OrbitExch buy other two (ask)
            poly_ask = poly_probs[poly_outcome].get("ask", 0)
            if poly_ask <= 0:
                continue

            orbit_asks_sum = 0
            orbit_legs = []
            valid = True

            for orbit_outcome in other_outcomes:
                orbit_ask = orbit_probs[orbit_outcome].get("ask", 0)
                if orbit_ask <= 0:
                    valid = False
                    break
                orbit_asks_sum += orbit_ask
                orbit_legs.append(
                    ArbitrageLeg(
                        venue=ArbitrageVenue.ORBITEXCH,
                        market_type=orbit_outcome,
                        action=ArbitrageAction.BUY,
                        probability=orbit_ask,
                        raw_odds=orbit_probs[orbit_outcome].get("raw_ask", 0),
                    )
                )

            if not valid:
                continue

            total_prob = poly_ask + orbit_asks_sum
            if total_prob < 100:
                # 返水到 Polymarket
                rebate_rate = (100 - total_prob) / poly_ask

                direction = ArbitrageDirection(
                    direction_id=f"{context.pair_id}_poly_{poly_outcome}_orbit_others",
                    legs=[
                        ArbitrageLeg(
                            venue=ArbitrageVenue.POLYMARKET,
                            market_type=poly_outcome,
                            action=ArbitrageAction.BUY,
                            probability=poly_ask,
                            raw_odds=poly_ask / 100,
                        ),
                        *orbit_legs,
                    ],
                    total_probability=total_prob,
                    rebate_rate=rebate_rate,
                    rebate_venue=ArbitrageVenue.POLYMARKET,
                    rebate_market=poly_outcome,
                    description=f"Buy Polymarket {poly_outcome} + OrbitExch {'+'.join(other_outcomes)}",
                )
                context.add_direction(direction)
                directions_info.append(direction.to_dict())

        details["directions"] = directions_info
