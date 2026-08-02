"""Execution reconciliation 报告的乐观并发快照。"""

from __future__ import annotations

from dataclasses import dataclass

from src.arbitrage.common.open_orders import orders_digest
from src.arbitrage.common.positions import positions_digest


@dataclass(frozen=True, slots=True)
class ReconciliationStateSnapshot:
    """远端请求发出前的账户级本地执行状态摘要。"""

    account_id: object
    venue: object
    order_digest: str
    position_digest: str | None
    realized_revision: int | None

    @classmethod
    def capture(cls, client, *, include_positions: bool) -> ReconciliationStateSnapshot:
        ledger = getattr(client, "_realized_pnl_ledger", None)
        return cls(
            account_id=client.account_id,
            venue=client.venue,
            order_digest=orders_digest(
                client._cache.orders(venue=client.venue, account_id=client.account_id),
            ),
            position_digest=(
                positions_digest(
                    client._cache.positions(venue=client.venue, account_id=client.account_id),
                )
                if include_positions
                else None
            ),
            realized_revision=(
                ledger.revision(client.account_id)
                if include_positions and ledger is not None
                else None
            ),
        )

    def is_current(self, client) -> bool:
        return self == self.capture(client, include_positions=self.position_digest is not None)


class GuardedReports(list):
    """携带拉取前本地状态与可选 deferred payload 的 NT report 列表。"""

    def __init__(self, reports, *, snapshot: ReconciliationStateSnapshot, payload=None) -> None:
        super().__init__(reports)
        self.snapshot = snapshot
        self.payload = payload


def attach_reconciliation_snapshot(report, snapshot: ReconciliationStateSnapshot):
    """把快照附到单份 NT report，供 QueryOrder 应用前校验。"""
    if report is not None:
        report._arb_reconciliation_snapshot = snapshot
    return report
