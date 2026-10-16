"""按订单 magic 阻止同 pair 的同类型活动订单重复生成。"""

from __future__ import annotations

import logging

from src.arbitrage.common.opportunity import order_magic
from src.arbitrage.strategy.checks.quote_legs import pair_instrument_ids
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


ORDER_MAGIC_SCRATCH = "order_magic"
_CLOSED_STATUSES = {
    "CANCELED",
    "DENIED",
    "EXPIRED",
    "FILLED",
    "REJECTED",
}
_LOG = logging.getLogger(__name__)


class MagicOrderGateAction(Action):
    """同 pair 已有相同 magic 的非终态订单时清空本分支下单输出。"""

    def __init__(self, magic: str, conflicts: list[str] | None = None) -> None:
        if not isinstance(magic, str) or not magic.strip():
            raise ValueError("magic_order_gate: magic must be a non-empty string")
        self._magic = magic.strip()
        if conflicts is not None and not isinstance(conflicts, list):
            raise ValueError("magic_order_gate: conflicts must be a list of non-empty strings")
        normalized_conflicts = set()
        for conflict in conflicts or ():
            if not isinstance(conflict, str) or not conflict.strip():
                raise ValueError(
                    "magic_order_gate: conflicts must be a list of non-empty strings",
                )
            normalized_conflicts.add(conflict.strip())
        normalized_conflicts.add(self._magic)
        self._conflicts = frozenset(normalized_conflicts)

    async def execute(self, ctx: EvalContext) -> None:
        ctx.scratch.pop(ORDER_MAGIC_SCRATCH, None)
        if ctx.cache is None or ctx.pair_registry is None:
            _LOG.warning(
                f"MagicOrderGate: pair={ctx.pair_id} magic={self._magic} "
                "missing cache/pair_registry, blocked",
            )
            _block_submission(ctx)
            return
        try:
            orders = _pair_orders(ctx)
        except (AttributeError, TypeError, ValueError):
            _LOG.warning(
                f"MagicOrderGate: pair={ctx.pair_id} magic={self._magic} "
                "failed to read pair orders, blocked",
                exc_info=True,
            )
            _block_submission(ctx)
            return
        duplicate = next(
            (
                order
                for order in orders
                if _is_active(order) and order_magic(order) in self._conflicts
            ),
            None,
        )
        if duplicate is not None:
            duplicate_magic = order_magic(duplicate)
            _LOG.info(
                f"MagicOrderGate: pair={ctx.pair_id} magic={self._magic} "
                f"conflict={duplicate_magic} blocked "
                f"by order={getattr(duplicate, 'client_order_id', None)}",
            )
            _block_submission(ctx)
            return
        ctx.scratch[ORDER_MAGIC_SCRATCH] = self._magic


def _pair_orders(ctx: EvalContext) -> list:
    orders = []
    seen = set()
    for instrument_id in pair_instrument_ids(ctx):
        for order in ctx.cache.orders(instrument_id=instrument_id) or ():
            key = str(getattr(order, "client_order_id", "") or "") or id(order)
            if key in seen:
                continue
            seen.add(key)
            orders.append(order)
    return orders


def _is_active(order) -> bool:
    closed = getattr(order, "is_closed", None)
    if callable(closed):
        closed = closed()
    if isinstance(closed, bool):
        return not closed
    status = getattr(order, "status", None)
    status_name = str(getattr(status, "name", status) or "").upper()
    return status_name not in _CLOSED_STATUSES


def _block_submission(ctx: EvalContext) -> None:
    ctx.scratch["legs"] = []
    ctx.scratch["candidates"] = []
    ctx.scratch.pop("selected_candidate", None)
    ctx.scratch.pop("execution_plan", None)
