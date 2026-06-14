"""套利 opportunity metadata 契约。

Strategy 写入 `Order.tags`;Risk / Execution 只读。保持解析逻辑一份,避免三处各自
硬编码 tag 前缀。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


TAG_PREFIX = "arb:"
RISK_LEG_DENIED_TOPIC = "risk.opportunity.leg_denied"


@dataclass(frozen=True, slots=True)
class OpportunityMeta:
    opportunity_id: str
    pair_id: str
    leg_key: str
    expected_legs: tuple[str, ...]
    intent: str = "arbitrage"


def new_opportunity_id() -> str:
    return f"ARB-OPP-{uuid4().hex[:12]}"


def tags_from_meta(meta: OpportunityMeta) -> list[str]:
    tags = [
        f"{TAG_PREFIX}opportunity_id={meta.opportunity_id}",
        f"{TAG_PREFIX}pair_id={meta.pair_id}",
        f"{TAG_PREFIX}leg_key={meta.leg_key}",
        f"{TAG_PREFIX}expected_legs={','.join(meta.expected_legs)}",
        f"{TAG_PREFIX}intent={meta.intent}",
    ]
    return tags


def meta_from_tags(tags) -> OpportunityMeta | None:
    values = _tag_values(tags)
    opportunity_id = values.get("opportunity_id")
    pair_id = values.get("pair_id")
    leg_key = values.get("leg_key")
    expected_raw = values.get("expected_legs")
    if not opportunity_id or not pair_id or not leg_key or not expected_raw:
        return None
    expected = tuple(part for part in expected_raw.split(",") if part)
    if leg_key not in expected:
        return None
    return OpportunityMeta(
        opportunity_id=opportunity_id,
        pair_id=pair_id,
        leg_key=leg_key,
        expected_legs=expected,
        intent=values.get("intent", "arbitrage"),
    )


def meta_from_order(order) -> OpportunityMeta | None:
    return meta_from_tags(getattr(order, "tags", None) or [])


def order_intent(order) -> str:
    values = _tag_values(getattr(order, "tags", None) or [])
    return values.get("intent", "arbitrage")


def _tag_values(tags) -> dict[str, str]:
    values: dict[str, str] = {}
    for tag in tags or []:
        if not isinstance(tag, str) or not tag.startswith(TAG_PREFIX) or "=" not in tag:
            continue
        key, value = tag[len(TAG_PREFIX):].split("=", 1)
        values[key] = value
    return values
