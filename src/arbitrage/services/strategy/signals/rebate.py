"""
返水率信号 (Rebate Signal)

计算跨平台套利返水率。

套利计算逻辑：
1. 转换赔率为概率：
   - OrbitExch: probability = 100 / decimal_odds
     - back (支持价) = ask (买入价)
     - lay (反对价) = bid (卖出价)
   - Polymarket: probability = value * 100
     - bid = 卖出价
     - ask = 买入价

2. 返水方向组合：
   - 返水到某个方向（如 home），而非某个平台
   - 非返水方向选最优价格：min(Polymarket ask, OrbitExch ask)
   - 返水方向决定返水平台：选哪个平台的 ask，就返水到哪个平台

3. 2-way 市场（4 种组合）：
   - 返水到 home (OrbitExch): Orbit home + min(P/O away)
   - 返水到 home (Polymarket): Poly home + min(P/O away)
   - 返水到 away (OrbitExch): Orbit away + min(P/O home)
   - 返水到 away (Polymarket): Poly away + min(P/O home)

4. 3-way 市场（6 种组合）：
   - 返水到 home (OrbitExch): Orbit home + min(P/O draw) + min(P/O away)
   - 返水到 home (Polymarket): Poly home + min(P/O draw) + min(P/O away)
   - 返水到 draw/away 同理...

5. 如果概率和 < 100，存在套利空间：
   - rebate_rate = (100 - total_prob) / rebate_direction_ask
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

        # 判断是否为 3-outcome 市场
        # 如果任一平台有 draw，说明是 3-outcome 市场，必须等待两边都有 draw
        poly_has_draw = "draw" in poly_markets
        orbit_has_draw = "draw" in orbit_markets

        if poly_has_draw or orbit_has_draw:
            # 3-outcome 市场：必须两边都有 draw 才能计算
            if not (poly_has_draw and orbit_has_draw):
                return SignalResult(
                    signal_name=self.name,
                    satisfied=False,
                    value=None,
                    details={
                        "error": "3-outcome market incomplete, waiting for all odds",
                        "poly_has_draw": poly_has_draw,
                        "orbit_has_draw": orbit_has_draw,
                        **details,
                    },
                )
            has_draw = True
        else:
            has_draw = False

        # 检查是否为 debug 模式（跳过 total_prob < 100 检查）
        skip_prob_check = False
        try:
            from src.arbitrage.services.debug import debug_manager
            if debug_manager.is_override_active("min_rebate_rate"):
                skip_prob_check = True
                details["debug_skip_prob_check"] = True
        except ImportError:
            pass

        # 计算套利方向
        if has_draw:
            self._calculate_3way_arbitrage(
                context, poly_probs, orbit_probs, rate_threshold, details, skip_prob_check
            )
        else:
            self._calculate_2way_arbitrage(
                context, poly_probs, orbit_probs, rate_threshold, details, skip_prob_check
            )

        # 获取最佳方向
        best_direction = context.get_best_direction()
        max_rebate = best_direction.rebate_rate if best_direction else None

        # 必须有至少一个方向，且返水率 >= 阈值
        satisfied = best_direction is not None and max_rebate >= rate_threshold

        return SignalResult(
            signal_name=self.name,
            satisfied=satisfied,
            value=round(max_rebate, 4) if max_rebate is not None else None,
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
        skip_prob_check: bool = False,
    ) -> None:
        """
        计算 2-outcome 市场的套利方向

        套利组合（返水到某个方向，非返水方向选最优价格）：
        - 返水到 home (OrbitExch): Orbit home + min(Poly away, Orbit away)
        - 返水到 home (Polymarket): Poly home + min(Poly away, Orbit away)
        - 返水到 away (OrbitExch): Orbit away + min(Poly home, Orbit home)
        - 返水到 away (Polymarket): Poly away + min(Poly home, Orbit home)

        Args:
            skip_prob_check: Debug 模式下跳过 total_prob < 100 检查
        """
        directions_info = []
        outcomes = ["home", "away"]

        # 检查必要数据（每个方向至少在一边存在）
        for outcome in outcomes:
            if outcome not in poly_probs and outcome not in orbit_probs:
                return

        # 遍历每个返水方向
        for rebate_outcome in outcomes:
            other_outcome = "away" if rebate_outcome == "home" else "home"

            # 获取非返水方向的最优价格
            poly_other_ask = poly_probs.get(other_outcome, {}).get("ask", 0)
            orbit_other_ask = orbit_probs.get(other_outcome, {}).get("ask", 0)

            missing_other = False
            if poly_other_ask <= 0 and orbit_other_ask <= 0:
                continue
            elif poly_other_ask <= 0:
                # 仅 OrbitExch 有价格
                best_other_ask = orbit_other_ask
                best_other_venue = ArbitrageVenue.ORBITEXCH
                best_other_raw_odds = orbit_probs[other_outcome].get("raw_ask", 0)
                missing_other = True
            elif orbit_other_ask <= 0:
                # 仅 Polymarket 有价格
                best_other_ask = poly_other_ask
                best_other_venue = ArbitrageVenue.POLYMARKET
                best_other_raw_odds = poly_other_ask / 100
                missing_other = True
            else:
                # 选择最优价格（最低概率 = 最低成本）
                if poly_other_ask <= orbit_other_ask:
                    best_other_ask = poly_other_ask
                    best_other_venue = ArbitrageVenue.POLYMARKET
                    best_other_raw_odds = poly_other_ask / 100
                else:
                    best_other_ask = orbit_other_ask
                    best_other_venue = ArbitrageVenue.ORBITEXCH
                    best_other_raw_odds = orbit_probs[other_outcome].get("raw_ask", 0)

            # 返水到 OrbitExch
            orbit_rebate_ask = orbit_probs.get(rebate_outcome, {}).get("ask", 0)
            if orbit_rebate_ask > 0:
                total_prob = orbit_rebate_ask + best_other_ask
                if skip_prob_check or total_prob < 100 or missing_other:
                    rebate_rate = (100 - total_prob) / orbit_rebate_ask
                    direction = ArbitrageDirection(
                        direction_id=f"{context.pair_id}_rebate_{rebate_outcome}_orbit",
                        legs=[
                            ArbitrageLeg(
                                venue=ArbitrageVenue.ORBITEXCH,
                                market_type=rebate_outcome,
                                action=ArbitrageAction.BUY,
                                probability=orbit_rebate_ask,
                                raw_odds=orbit_probs[rebate_outcome].get("raw_ask", 0),
                            ),
                            ArbitrageLeg(
                                venue=best_other_venue,
                                market_type=other_outcome,
                                action=ArbitrageAction.BUY,
                                probability=best_other_ask,
                                raw_odds=best_other_raw_odds,
                            ),
                        ],
                        total_probability=total_prob,
                        rebate_rate=rebate_rate,
                        rebate_venue=ArbitrageVenue.ORBITEXCH,
                        rebate_market=rebate_outcome,
                        description=f"Rebate to OrbitExch {rebate_outcome}",
                    )
                    context.add_direction(direction)
                    directions_info.append(direction.to_dict())

            # 返水到 Polymarket
            poly_rebate_ask = poly_probs.get(rebate_outcome, {}).get("ask", 0)
            if poly_rebate_ask > 0:
                total_prob = poly_rebate_ask + best_other_ask
                if skip_prob_check or total_prob < 100 or missing_other:
                    rebate_rate = (100 - total_prob) / poly_rebate_ask
                    direction = ArbitrageDirection(
                        direction_id=f"{context.pair_id}_rebate_{rebate_outcome}_poly",
                        legs=[
                            ArbitrageLeg(
                                venue=ArbitrageVenue.POLYMARKET,
                                market_type=rebate_outcome,
                                action=ArbitrageAction.BUY,
                                probability=poly_rebate_ask,
                                raw_odds=poly_rebate_ask / 100,
                            ),
                            ArbitrageLeg(
                                venue=best_other_venue,
                                market_type=other_outcome,
                                action=ArbitrageAction.BUY,
                                probability=best_other_ask,
                                raw_odds=best_other_raw_odds,
                            ),
                        ],
                        total_probability=total_prob,
                        rebate_rate=rebate_rate,
                        rebate_venue=ArbitrageVenue.POLYMARKET,
                        rebate_market=rebate_outcome,
                        description=f"Rebate to Polymarket {rebate_outcome}",
                    )
                    context.add_direction(direction)
                    directions_info.append(direction.to_dict())

        details["directions"] = directions_info

    def _calculate_3way_arbitrage(
        self,
        context: MatchContext,
        poly_probs: dict,
        orbit_probs: dict,
        rate_threshold: float,
        details: dict,
        skip_prob_check: bool = False,
    ) -> None:
        """
        计算 3-outcome 市场的套利方向

        套利组合（返水到某个方向，非返水方向选最优价格）：
        - 返水到 home (OrbitExch): Orbit home + min(P/O draw) + min(P/O away)
        - 返水到 home (Polymarket): Poly home + min(P/O draw) + min(P/O away)
        - 返水到 draw (OrbitExch): Orbit draw + min(P/O home) + min(P/O away)
        - 返水到 draw (Polymarket): Poly draw + min(P/O home) + min(P/O away)
        - 返水到 away (OrbitExch): Orbit away + min(P/O home) + min(P/O draw)
        - 返水到 away (Polymarket): Poly away + min(P/O home) + min(P/O draw)

        Args:
            skip_prob_check: Debug 模式下跳过 total_prob < 100 检查
        """
        directions_info = []
        outcomes = ["home", "draw", "away"]

        # 检查必要数据（每个方向至少在一边存在）
        for outcome in outcomes:
            if outcome not in poly_probs and outcome not in orbit_probs:
                return

        # 遍历每个返水方向
        for rebate_outcome in outcomes:
            other_outcomes = [o for o in outcomes if o != rebate_outcome]

            # 获取非返水方向的最优价格
            best_others = []
            other_legs = []
            valid = True
            missing_other = False

            for other_outcome in other_outcomes:
                poly_other_ask = poly_probs.get(other_outcome, {}).get("ask", 0)
                orbit_other_ask = orbit_probs.get(other_outcome, {}).get("ask", 0)

                if poly_other_ask <= 0 and orbit_other_ask <= 0:
                    valid = False
                    break
                elif poly_other_ask <= 0:
                    best_ask = orbit_other_ask
                    best_venue = ArbitrageVenue.ORBITEXCH
                    best_raw_odds = orbit_probs[other_outcome].get("raw_ask", 0)
                    missing_other = True
                elif orbit_other_ask <= 0:
                    best_ask = poly_other_ask
                    best_venue = ArbitrageVenue.POLYMARKET
                    best_raw_odds = poly_other_ask / 100
                    missing_other = True
                else:
                    # 选择最优价格（最低概率 = 最低成本）
                    if poly_other_ask <= orbit_other_ask:
                        best_ask = poly_other_ask
                        best_venue = ArbitrageVenue.POLYMARKET
                        best_raw_odds = poly_other_ask / 100
                    else:
                        best_ask = orbit_other_ask
                        best_venue = ArbitrageVenue.ORBITEXCH
                        best_raw_odds = orbit_probs[other_outcome].get("raw_ask", 0)

                best_others.append(best_ask)
                other_legs.append(
                    ArbitrageLeg(
                        venue=best_venue,
                        market_type=other_outcome,
                        action=ArbitrageAction.BUY,
                        probability=best_ask,
                        raw_odds=best_raw_odds,
                    )
                )

            if not valid:
                continue

            best_others_sum = sum(best_others)

            # 返水到 OrbitExch
            orbit_rebate_ask = orbit_probs.get(rebate_outcome, {}).get("ask", 0)
            if orbit_rebate_ask > 0:
                total_prob = orbit_rebate_ask + best_others_sum
                if skip_prob_check or total_prob < 100 or missing_other:
                    rebate_rate = (100 - total_prob) / orbit_rebate_ask
                    direction = ArbitrageDirection(
                        direction_id=f"{context.pair_id}_rebate_{rebate_outcome}_orbit",
                        legs=[
                            ArbitrageLeg(
                                venue=ArbitrageVenue.ORBITEXCH,
                                market_type=rebate_outcome,
                                action=ArbitrageAction.BUY,
                                probability=orbit_rebate_ask,
                                raw_odds=orbit_probs[rebate_outcome].get("raw_ask", 0),
                            ),
                            *other_legs,
                        ],
                        total_probability=total_prob,
                        rebate_rate=rebate_rate,
                        rebate_venue=ArbitrageVenue.ORBITEXCH,
                        rebate_market=rebate_outcome,
                        description=f"Rebate to OrbitExch {rebate_outcome}",
                    )
                    context.add_direction(direction)
                    directions_info.append(direction.to_dict())

            # 返水到 Polymarket
            poly_rebate_ask = poly_probs.get(rebate_outcome, {}).get("ask", 0)
            if poly_rebate_ask > 0:
                total_prob = poly_rebate_ask + best_others_sum
                if skip_prob_check or total_prob < 100 or missing_other:
                    rebate_rate = (100 - total_prob) / poly_rebate_ask
                    direction = ArbitrageDirection(
                        direction_id=f"{context.pair_id}_rebate_{rebate_outcome}_poly",
                        legs=[
                            ArbitrageLeg(
                                venue=ArbitrageVenue.POLYMARKET,
                                market_type=rebate_outcome,
                                action=ArbitrageAction.BUY,
                                probability=poly_rebate_ask,
                                raw_odds=poly_rebate_ask / 100,
                            ),
                            *other_legs,
                        ],
                        total_probability=total_prob,
                        rebate_rate=rebate_rate,
                        rebate_venue=ArbitrageVenue.POLYMARKET,
                        rebate_market=rebate_outcome,
                        description=f"Rebate to Polymarket {rebate_outcome}",
                    )
                    context.add_direction(direction)
                    directions_info.append(direction.to_dict())

        details["directions"] = directions_info
