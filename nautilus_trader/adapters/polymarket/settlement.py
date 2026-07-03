"""
PolymarketSettlement —— merge / redeem 编排(Q18c 三层结构的中间层)。

结构(详细设计 `docs/arbitrage/architectures/execution/architecture.md §4.6`):
    PM ExecClient 子类(宿主+触发:NT position reconcile 内调)
      └─ PolymarketSettlement(本类,编排:按 condition 分组 / min 取量 / redeemable 门控)
           └─ contract.py:PolymarketContractService(链上 IO:Builder Relayer)

2026-07-03 移入 adapter 目录:Settlement 100% PM 特有,触发点在 PM reconciliate,
代码位置反映真实依赖(选项 A)。

- merge:同 condition ≥2 持仓 → `merge_positions(condition, min(sizes), neg_risk=any)`。
- redeem:任一持仓 `redeemable=True` → `redeem_positions(...)`(negRisk 传 amounts)。
- `TxResult` 失败**只 log,不作健康判据**,下次 tick 幂等重试(详见 §4.6)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field

from nautilus_trader.adapters.polymarket.contract import PolymarketContractService
from nautilus_trader.adapters.polymarket.contract import TxResult


@dataclass
class SettlementPosition:
    """settlement 需要的最小持仓视图(由 PM 健康检查从 /positions 原始响应映射)。"""

    condition_id: str
    size: float
    neg_risk: bool = False
    redeemable: bool = False


@dataclass
class SettlementResult:
    merges: list[TxResult] = field(default_factory=list)
    redeems: list[TxResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_operations(self) -> bool:
        return bool(self.merges or self.redeems)

    @property
    def summary(self) -> str:
        parts: list[str] = []
        if self.merges:
            parts.append(f"merge: {sum(m.success for m in self.merges)}/{len(self.merges)}")
        if self.redeems:
            parts.append(f"redeem: {sum(r.success for r in self.redeems)}/{len(self.redeems)}")
        if self.errors:
            parts.append(f"errors: {len(self.errors)}")
        return ", ".join(parts) if parts else "no operations"


class PolymarketSettlement:
    def __init__(
        self,
        contract_service: PolymarketContractService,
        *,
        merge_enabled: bool = True,
        claim_enabled: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        self._contract = contract_service
        self._merge_enabled = merge_enabled
        self._claim_enabled = claim_enabled
        self._log = logger or logging.getLogger(self.__class__.__name__)

    async def run(self, positions: list[SettlementPosition]) -> SettlementResult:
        """对传入持仓执行 merge + redeem。异常吞进 result.errors,不抛(健康检查 tick 不被打断)。"""
        result = SettlementResult()
        live = [p for p in positions if p.size > 0]
        if not live:
            return result

        by_condition: dict[str, list[SettlementPosition]] = {}
        for pos in live:
            if not pos.condition_id:
                self._log.warning("settlement position missing condition_id, skipping")
                continue
            by_condition.setdefault(pos.condition_id, []).append(pos)

        if self._merge_enabled:
            await self._run_merges(by_condition, result)
        if self._claim_enabled:
            await self._run_redeems(by_condition, result)
        return result

    async def _run_merges(self, by_condition, result: SettlementResult) -> None:
        for condition_id, positions in by_condition.items():
            if len(positions) < 2:
                continue  # merge 需同 condition ≥2 outcome 持仓
            amount = min(p.size for p in positions)
            if amount <= 0:
                continue
            neg_risk = any(p.neg_risk for p in positions)
            try:
                tx = await self._contract.merge_positions(
                    condition_id=condition_id, amount=amount, neg_risk=neg_risk,
                )
                result.merges.append(tx)
                if not tx.success:
                    self._log.warning(f"Merge failed: condition={condition_id[:16]}..., {tx.message}")
            except Exception as e:  # noqa: BLE001 — 失败仅记录,下次 tick 幂等重试
                result.errors.append(f"merge {condition_id[:16]}: {e}")
                self._log.error(f"Merge error: {e}")

    async def _run_redeems(self, by_condition, result: SettlementResult) -> None:
        for condition_id, positions in by_condition.items():
            if sum(p.size for p in positions) <= 0:
                continue
            if not any(p.redeemable for p in positions):
                continue  # 用 Data API 的 redeemable 标记门控
            neg_risk = any(p.neg_risk for p in positions)
            try:
                if neg_risk:
                    tx = await self._contract.redeem_positions(
                        condition_id=condition_id, neg_risk=True,
                        amounts=[p.size for p in positions],
                    )
                else:
                    tx = await self._contract.redeem_positions(
                        condition_id=condition_id, neg_risk=False,
                    )
                result.redeems.append(tx)
                if not tx.success:
                    self._log.warning(f"Redeem failed: condition={condition_id[:16]}..., {tx.message}")
            except Exception as e:  # noqa: BLE001
                result.errors.append(f"redeem {condition_id[:16]}: {e}")
                self._log.error(f"Redeem error: {e}")
