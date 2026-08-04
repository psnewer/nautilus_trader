"""套利 opportunity metadata 契约。

Strategy 写入 `Order.tags`;Risk / Execution 只读。保持解析逻辑一份,避免三处各自
硬编码 tag 前缀。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


TAG_PREFIX = "arb:"
RISK_LEG_DENIED_TOPIC = "risk.opportunity.leg_denied"
CANCEL_OPPORTUNITY_PARAM = "arb_cancel_opportunity"


@dataclass(frozen=True, slots=True)
class OpportunityMeta:
    opportunity_id: str
    pair_id: str
    leg_key: str
    expected_legs: tuple[str, ...]
    positions_digest: str | None = None  # #317:open_orders_digest 已删(barrier 仅校验 position)
    intent: str = "arbitrage"
    venue_required_balance: float | None = None
    enable_timeout: bool | None = None


@dataclass(frozen=True, slots=True)
class CancelOpportunityMeta:
    opportunity_id: str
    pair_id: str
    cancel_key: str
    expected_cancels: tuple[str, ...]


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
    if meta.positions_digest is not None:
        tags.append(f"{TAG_PREFIX}positions_digest={meta.positions_digest}")
    if meta.venue_required_balance is not None:
        tags.append(f"{TAG_PREFIX}venue_required_balance={meta.venue_required_balance}")
    if meta.enable_timeout is not None:
        tags.append(f"{TAG_PREFIX}enable_timeout={str(meta.enable_timeout).lower()}")
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
    required_balance = _optional_float(values.get("venue_required_balance"))
    return OpportunityMeta(
        opportunity_id=opportunity_id,
        pair_id=pair_id,
        leg_key=leg_key,
        expected_legs=expected,
        positions_digest=values.get("positions_digest"),
        intent=values.get("intent", "arbitrage"),
        venue_required_balance=required_balance,
        enable_timeout=(
            values["enable_timeout"].lower() == "true"
            if values.get("enable_timeout", "").lower() in {"true", "false"}
            else None
        ),
    )


def meta_from_order(order) -> OpportunityMeta | None:
    return meta_from_tags(getattr(order, "tags", None) or [])


def order_intent(order) -> str:
    values = _tag_values(getattr(order, "tags", None) or [])
    return values.get("intent", "arbitrage")


def cancel_params_from_meta(meta: CancelOpportunityMeta) -> dict[str, object]:
    return {
        CANCEL_OPPORTUNITY_PARAM: {
            "opportunity_id": meta.opportunity_id,
            "pair_id": meta.pair_id,
            "cancel_key": meta.cancel_key,
            "expected_cancels": list(meta.expected_cancels),
        },
    }


def cancel_meta_from_command(command) -> CancelOpportunityMeta | None:
    params = getattr(command, "params", None) or {}
    raw = params.get(CANCEL_OPPORTUNITY_PARAM)
    if not isinstance(raw, dict):
        return None
    opportunity_id = str(raw.get("opportunity_id") or "")
    pair_id = str(raw.get("pair_id") or "")
    cancel_key = str(raw.get("cancel_key") or "")
    expected = tuple(str(value) for value in (raw.get("expected_cancels") or ()) if value)
    if (
        not opportunity_id
        or not pair_id
        or not cancel_key
        or cancel_key not in expected
        or len(expected) != len(set(expected))
    ):
        return None
    return CancelOpportunityMeta(
        opportunity_id=opportunity_id,
        pair_id=pair_id,
        cancel_key=cancel_key,
        expected_cancels=expected,
    )


def _tag_values(tags) -> dict[str, str]:
    values: dict[str, str] = {}
    for tag in tags or []:
        if not isinstance(tag, str) or not tag.startswith(TAG_PREFIX) or "=" not in tag:
            continue
        key, value = tag[len(TAG_PREFIX):].split("=", 1)
        values[key] = value
    return values


def _optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
