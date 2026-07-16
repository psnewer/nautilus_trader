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

from .config import ExecutionConfig
from nautilus_trader.adapters.polymarket.contract import PolymarketContractService, TxResult


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
            positions = await polymarket_client.fetch_positions()
            if not positions:
                self._log.info("No positions found for cleanup")
                return result

            pair_positions = [p for p in positions if p.size > 0]
            result = await self._do_cleanup(pair_positions, label=f"pair {pair_id}")

        except Exception as e:
            error_msg = f"Cleanup error for pair {pair_id}: {e}"
            self._log.error(error_msg)
            result.errors.append(error_msg)

        self._log.info(f"Cleanup complete for pair {pair_id}: {result.summary}")
        return result

    async def execute_global(self, polymarket_client) -> CleanupResult:
        """
        全局 cleanup：对所有 Polymarket 持仓执行 merge & claim
        不限定 pair_id。

        Args:
            polymarket_client: PolymarketOddsClient

        Returns:
            CleanupResult
        """
        result = CleanupResult()

        if not self._config.cleanup_enabled:
            return result

        self._log.info("Starting global post-session cleanup")

        try:
            positions = await polymarket_client.fetch_positions()
            if not positions:
                self._log.info("No positions found for global cleanup")
                return result

            all_positions = [p for p in positions if p.size > 0]
            result = await self._do_cleanup(all_positions, label="global")

        except Exception as e:
            error_msg = f"Global cleanup error: {e}"
            self._log.error(error_msg)
            result.errors.append(error_msg)

        self._log.info(f"Global cleanup complete: {result.summary}")
        return result

    async def _do_cleanup(self, positions: list, label: str = "") -> CleanupResult:
        """
        执行 cleanup 核心逻辑：merge & claim

        Args:
            positions: 有持仓量的 position 列表
            label: 日志标签

        Returns:
            CleanupResult
        """
        result = CleanupResult()

        if not positions:
            self._log.info(f"No positions with size > 0 ({label})")
            return result

        self._log.info(
            f"Found {len(positions)} positions ({label}): "
            + ", ".join(
                f"{p.market_type}({p.outcome})={p.size:.2f}"
                for p in positions
            )
        )

        # 按 condition_id 分组
        by_condition: dict[str, list] = {}
        for pos in positions:
            if not pos.condition_id:
                self._log.warning(
                    f"Position {pos.asset_id[:20]}... missing condition_id, skipping"
                )
                continue
            by_condition.setdefault(pos.condition_id, []).append(pos)

        # Merge: 同一 condition 下两个 outcome 都有持仓
        if self._config.cleanup_merge_enabled:
            for condition_id, cond_positions in by_condition.items():
                if len(cond_positions) < 2:
                    continue

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

        # Claim/Redeem: 使用 Data API 返回的 redeemable 标记
        if self._config.cleanup_claim_enabled:
            for condition_id, cond_positions in by_condition.items():
                total_size = sum(p.size for p in cond_positions)
                if total_size <= 0:
                    continue

                neg_risk = any(p.neg_risk for p in cond_positions)

                # 检查是否有任一 position 标记为 redeemable
                is_redeemable = any(
                    getattr(p, "redeemable", False) for p in cond_positions
                )
                if not is_redeemable:
                    self._log.debug(
                        f"Market not redeemable: condition={condition_id[:16]}..."
                    )
                    continue

                self._log.info(
                    f"Redeem opportunity: condition={condition_id[:16]}..., "
                    f"neg_risk={neg_risk}"
                )

                tx_result = await self._contract.redeem_positions(
                    condition_id=condition_id,
                    neg_risk=neg_risk,
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

        return result
