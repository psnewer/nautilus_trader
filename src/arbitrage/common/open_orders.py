"""订单集合稳定摘要。

`orders_digest(orders)` 供各 venue reconciliation 的乐观并发校验(execution §4.6)。
只保存不可变字段，不持有会被 NT Cache 原地更新的 Order 引用。
(#317:pair 级 `pair_open_orders_digest` 已删 —— barrier 不再做 open-order 校验,承 #316 per-pair ≤1。)
"""

from __future__ import annotations

import hashlib
import json


def orders_digest(orders) -> str:
    """返回任意订单集合的稳定摘要，供跨 await 状态一致性校验。"""
    fingerprints = [_order_fingerprint(order) for order in orders or ()]
    payload = json.dumps(sorted(fingerprints), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _order_fingerprint(order) -> tuple[str, ...]:
    return (
        str(getattr(order, "client_order_id", "") or ""),
        str(getattr(order, "venue_order_id", "") or ""),
        str(getattr(order, "instrument_id", "") or ""),
        _enum_name(getattr(order, "side", None)),
        _enum_name(getattr(order, "status", None)),
        _value_text(getattr(order, "quantity", None)),
        _value_text(getattr(order, "filled_qty", None)),
        _value_text(getattr(order, "leaves_qty", None)),
        _order_price_text(order),
        _callable_value_text(getattr(order, "event_count", None)),
        _value_text(getattr(order, "ts_last", None)),
    )


def _enum_name(value) -> str:
    return str(getattr(value, "name", value) or "")


def _value_text(value) -> str:
    if value is None:
        return ""
    raw = getattr(value, "raw", None)
    precision = getattr(value, "precision", None)
    if raw is not None:
        return f"{raw}:{precision}"
    return str(value)


def _callable_value_text(value) -> str:
    return _value_text(value() if callable(value) else value)


def _order_price_text(order) -> str:
    has_price = getattr(order, "has_price", False)
    if callable(has_price):
        has_price = has_price()
    return _value_text(getattr(order, "price", None)) if has_price else ""
