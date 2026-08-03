"""策略树生成、Evaluator 统一分发的执行计划。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class PreparedOrder:
    """已经完成策略侧转换、可直接交给 submitter 的订单。"""

    spec: dict
    venue: str
    role: str


@dataclass(frozen=True)
class ExecutionPlan:
    """树内 Action 的终点；本对象本身不产生执行副作用。"""

    kind: str
    pair_id: str
    orders: tuple[PreparedOrder, ...] = ()
    reason: str | None = None

    @classmethod
    def submit(cls, pair_id: str, orders: list[PreparedOrder]) -> ExecutionPlan:
        return cls(kind="submit", pair_id=pair_id, orders=tuple(orders))

    @classmethod
    def cancel_pair(
        cls,
        pair_id: str,
        reason: str | None,
    ) -> ExecutionPlan:
        return cls(
            kind="cancel_pair",
            pair_id=pair_id,
            reason=reason,
        )


async def dispatch_execution_plan(
    plan: ExecutionPlan,
    *,
    submitter,
    pair_order_canceler,
    log,
    source: str,
) -> None:
    """分发唯一胜出的计划；submit/cancel 都继续走既有 NT + barrier 通路。"""
    if plan.kind == "cancel_pair":
        if pair_order_canceler is None:
            log.info(
                f"ExecutionPlan[cancel-smoke]: pair={plan.pair_id} "
                f"source={source} reason={plan.reason} orders=0",
            )
            return
        count = pair_order_canceler(plan.pair_id)
        log.info(
            f"ExecutionPlan[cancel]: pair={plan.pair_id} "
            f"source={source} reason={plan.reason} orders={count}",
        )
        return

    if plan.kind != "submit":
        raise ValueError(f"unsupported execution plan kind: {plan.kind!r}")

    log.info(
        f"ExecutionPlan[submit]: pair={plan.pair_id} "
        f"source={source} orders={len(plan.orders)}",
    )
    if submitter is not None:
        await asyncio.gather(*(submitter(order.spec) for order in plan.orders))
        return

    for order in plan.orders:
        spec = order.spec
        log.info(
            f"  would submit: instrument={spec['instrument_id']} side={spec['side']} "
            f"role={order.role} venue={order.venue} qty={spec['qty']:.4f} "
            f"price={spec['price']}",
        )
