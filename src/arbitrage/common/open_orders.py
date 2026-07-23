"""Pair 级挂单基线。

Strategy 在评估开始时记录摘要，Execution barrier release 前用同一函数重算并比较。
只保存不可变字段，不持有会被 NT Cache 原地更新的 Order 引用。
"""

from __future__ import annotations

import hashlib
import json

from nautilus_trader.model.identifiers import InstrumentId


def pair_open_orders_digest(cache, instrument_ids) -> str:
    """返回指定 instruments 当前 open orders 的稳定摘要。"""
    fingerprints = []
    for raw_instrument_id in sorted({str(value) for value in instrument_ids}):
        instrument_id = InstrumentId.from_str(raw_instrument_id)
        for order in cache.orders_open(instrument_id=instrument_id) or ():
            fingerprints.append(_order_fingerprint(order))
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


def _order_price_text(order) -> str:
    has_price = getattr(order, "has_price", False)
    if callable(has_price):
        has_price = has_price()
    return _value_text(getattr(order, "price", None)) if has_price else ""
