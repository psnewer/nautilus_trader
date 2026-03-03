"""
Post-Session Cleanup: Merge & Claim

Session 结束后的 Polymarket 仓位收尾：
1. 查询当前 pair 所有 Polymarket 持仓
2. 按 conditionId 分组，找可 merge 的仓位（同一 condition 下两个 outcome 都有持仓）
3. 执行 merge（合并回 USDC.e）
4. 查询市场是否已结算，执行 redeem（赎回 winning tokens）
"""

import logging
from dataclasses import dataclass, field

import httpx

from .config import ExecutionConfig
from .polymarket_contract import PolymarketContractService, TxResult


@dataclass
class CleanupResult:
    """收尾操作结果"""
    merges: list[TxResult] = field(default_factory=list)
    redeems: list[TxResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_operations(self) -> bool:
        return bool(self.merges or self.redeems)

    @property
    def summary(self) -> str:
        merge_ok = sum(1 for m in self.merges if m.success)
        redeem_ok = sum(1 for r in self.redeems if r.success)
        parts = []
        if self.merges:
            parts.append(f"merge: {merge_ok}/{len(self.merges)}")
        if self.redeems:
            parts.append(f"redeem: {redeem_ok}/{len(self.redeems)}")
        if self.errors:
            parts.append(f"errors: {len(self.errors)}")
        return ", ".join(parts) if parts else "no operations"


class PostSessionCleanup:
    """
    Session 结束后的 Polymarket 仓位收尾

    在 session 完成后自动执行，尝试：
    1. 合并同一 condition 下的对立仓位
    2. 赎回已结算市场的 winning tokens
    """

    def __init__(
        self,
        config: ExecutionConfig,
        contract_service: PolymarketContractService,
        logger: logging.Logger | None = None,
    ):
        self._config = config
        self._contract = contract_service
        self._log = logger or logging.getLogger(self.__class__.__name__)

    async def execute(self, session, polymarket_client) -> CleanupResult:
        """
        执行 post-session cleanup

        Args:
            session: ExecutionSession
            polymarket_client: PolymarketOddsClient

        Returns:
            CleanupResult
        """
        result = CleanupResult()

        if not self._config.cleanup_enabled:
            self._log.debug("Cleanup disabled")
            return result

        pair_id = session.pair_id
        self._log.info(f"Starting post-session cleanup for pair {pair_id}")

        try:
            # 1. 刷新持仓
            positions = await polymarket_client.fetch_positions()
            if not positions:
                self._log.info(f"No positions found for cleanup")
                return result

            # 过滤属于当前 pair 的持仓
            pair_positions = [
                p for p in positions
                if p.event_id == pair_id and p.size > 0
            ]

            if not pair_positions:
                self._log.info(f"No positions for pair {pair_id}")
                return result

            self._log.info(
                f"Found {len(pair_positions)} positions for pair {pair_id}: "
                + ", ".join(
                    f"{p.market_type}({p.outcome})={p.size:.2f}"
                    for p in pair_positions
                )
            )

            # 2. 按 condition_id 分组
            by_condition: dict[str, list] = {}
            for pos in pair_positions:
                if not pos.condition_id:
                    self._log.warning(
                        f"Position {pos.asset_id[:20]}... missing condition_id, skipping"
                    )
                    continue
                by_condition.setdefault(pos.condition_id, []).append(pos)

            # 3. Merge: 同一 condition 下两个 outcome 都有持仓
            if self._config.cleanup_merge_enabled:
                for condition_id, cond_positions in by_condition.items():
                    if len(cond_positions) < 2:
                        continue

                    # 取所有仓位的最小 size 作为 merge 数量
                    sizes = [p.size for p in cond_positions]
                    merge_amount = min(sizes)

                    if merge_amount <= 0:
                        continue

                    neg_risk = any(p.neg_risk for p in cond_positions)

                    self._log.info(
                        f"Merge opportunity: condition={condition_id[:16]}..., "
                        f"neg_risk={neg_risk}, amount={merge_amount:.2f}, "
                        f"positions={len(cond_positions)}"
                    )

                    tx_result = await self._contract.merge_positions(
                        condition_id=condition_id,
                        amount=merge_amount,
                        neg_risk=neg_risk,
                    )
                    result.merges.append(tx_result)

                    if tx_result.success:
                        self._log.info(
                            f"Merge success: condition={condition_id[:16]}..., "
                            f"amount={merge_amount:.2f}, tx={tx_result.tx_hash}"
                        )
                    else:
                        self._log.warning(
                            f"Merge failed: condition={condition_id[:16]}..., "
                            f"error={tx_result.message}"
                        )

            # 4. Claim/Redeem: 检查市场是否已结算
            if self._config.cleanup_claim_enabled:
                for condition_id, cond_positions in by_condition.items():
                    # 检查是否有持仓可赎回
                    total_size = sum(p.size for p in cond_positions)
                    if total_size <= 0:
                        continue

                    neg_risk = any(p.neg_risk for p in cond_positions)

                    # 查询市场结算状态
                    event_id = cond_positions[0].event_id
                    is_resolved = await self._check_market_resolved(event_id)

                    if not is_resolved:
                        self._log.debug(
                            f"Market not resolved: condition={condition_id[:16]}..."
                        )
                        continue

                    self._log.info(
                        f"Redeem opportunity: condition={condition_id[:16]}..., "
                        f"neg_risk={neg_risk}, event={event_id}"
                    )

                    if neg_risk:
                        amounts = [p.size for p in cond_positions]
                        tx_result = await self._contract.redeem_positions(
                            condition_id=condition_id,
                            neg_risk=True,
                            amounts=amounts,
                        )
                    else:
                        tx_result = await self._contract.redeem_positions(
                            condition_id=condition_id,
                            neg_risk=False,
                        )

                    result.redeems.append(tx_result)

                    if tx_result.success:
                        self._log.info(
                            f"Redeem success: condition={condition_id[:16]}..., "
                            f"tx={tx_result.tx_hash}"
                        )
                    else:
                        self._log.warning(
                            f"Redeem failed: condition={condition_id[:16]}..., "
                            f"error={tx_result.message}"
                        )

        except Exception as e:
            error_msg = f"Cleanup error for pair {pair_id}: {e}"
            self._log.error(error_msg)
            result.errors.append(error_msg)

        self._log.info(f"Cleanup complete for pair {pair_id}: {result.summary}")
        return result

    async def _check_market_resolved(self, event_id: str) -> bool:
        """
        查询市场是否已结算

        通过 Gamma API 检查 market.umaResolutionStatus

        Args:
            event_id: Polymarket event ID

        Returns:
            是否已结算
        """
        try:
            url = f"https://gamma-api.polymarket.com/events/{event_id}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()

                markets = data.get("markets", [])
                if not markets:
                    return False

                # 所有 market 都已 resolved 才算 resolved
                for market in markets:
                    status = market.get("umaResolutionStatus", "")
                    if status != "resolved":
                        return False

                return True

        except Exception as e:
            self._log.warning(f"Failed to check market resolution for {event_id}: {e}")
            return False
