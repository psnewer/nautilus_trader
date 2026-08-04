"""Execution reconciliation 报告的乐观并发快照(#318:per-pair、order/position 拆分)。

#318 起快照按 **instrument 捕获**(capture 无需 registry),engine 侧经 `PairRegistry` 聚合到
pair 判定(`is_current_for_instruments`)。order 快照只含 order digest、position 快照只含 position
digest(含 realized_pnl);不再有账户级 `realized_revision`(其职责被 position digest 覆盖)。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from src.arbitrage.common.open_orders import orders_digest
from src.arbitrage.common.positions import positions_digest


_EMPTY_ORDER_DIGEST = orders_digest([])
_EMPTY_POSITION_DIGEST = positions_digest([])


@dataclass(frozen=True, slots=True)
class ReconciliationStateSnapshot:
    """远端请求发出前的本地执行状态摘要,按 instrument 分格。

    `kind="order"` → 每 instrument 的 open/all orders 摘要;`kind="position"` → 每 instrument 的
    position 摘要(含 realized_pnl)。判定按 pair 聚合:该 pair 全部 instrument 的分格摘要都未变才算 current。
    """

    account_id: object
    venue: object
    kind: str  # "order" | "position"
    per_instrument: tuple[tuple[str, str], ...]  # sorted (instrument_id_str, digest)

    @classmethod
    def capture(cls, client, *, kind: str) -> ReconciliationStateSnapshot:
        groups = _group_by_instrument(client, kind)
        digest_fn = orders_digest if kind == "order" else positions_digest
        per_instrument = tuple(
            sorted((iid, digest_fn(items)) for iid, items in groups.items())
        )
        return cls(
            account_id=client.account_id,
            venue=client.venue,
            kind=kind,
            per_instrument=per_instrument,
        )

    def is_current_for_instruments(self, client, instrument_ids) -> bool:
        """该组 instrument 的分格摘要是否都与捕获时一致(供 engine 按 pair 聚合调用)。"""
        captured = dict(self.per_instrument)
        empty = _EMPTY_ORDER_DIGEST if self.kind == "order" else _EMPTY_POSITION_DIGEST
        groups = _group_by_instrument(client, self.kind)
        digest_fn = orders_digest if self.kind == "order" else positions_digest
        for instrument_id in instrument_ids:
            key = str(instrument_id)
            current = digest_fn(groups.get(key, ()))
            if captured.get(key, empty) != current:
                return False
        return True


def _group_by_instrument(client, kind: str) -> dict[str, list]:
    if kind == "order":
        items = client._cache.orders(venue=client.venue, account_id=client.account_id)
    else:
        items = client._cache.positions(venue=client.venue, account_id=client.account_id)
    groups: dict[str, list] = defaultdict(list)
    for item in items or ():
        groups[str(item.instrument_id)].append(item)
    return groups


class GuardedReports(list):
    """携带拉取前本地状态与可选 deferred payload 的 NT report 列表。"""

    def __init__(self, reports, *, snapshot: ReconciliationStateSnapshot, payload=None) -> None:
        super().__init__(reports)
        self.snapshot = snapshot
        self.payload = payload


def attach_reconciliation_snapshot(report, snapshot: ReconciliationStateSnapshot):
    """把快照附到单份 NT report,供 per-report(按 pair)应用前校验。"""
    if report is not None:
        report._arb_reconciliation_snapshot = snapshot
    return report
