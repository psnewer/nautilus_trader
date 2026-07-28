"""Pair 级仓位基线。

Strategy 在评估开始时记录摘要，Execution barrier release 前用同一函数重算并比较。
只保存不可变字段，不持有会被 NT Cache 原地更新的 Position 引用。
"""

from __future__ import annotations

import hashlib
import json

from nautilus_trader.model.identifiers import InstrumentId


def pair_positions_digest(cache, instrument_ids) -> str:
    """返回指定 instruments 当前 positions 的稳定摘要。"""
    fingerprints = []
    for raw_instrument_id in sorted({str(value) for value in instrument_ids}):
        instrument_id = InstrumentId.from_str(raw_instrument_id)
        for position in cache.positions(instrument_id=instrument_id) or ():
            fingerprints.append(_position_fingerprint(position))
    payload = json.dumps(sorted(fingerprints), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _position_fingerprint(position) -> tuple[str, ...]:
    return (
        str(getattr(position, "id", "") or ""),
        str(getattr(position, "account_id", "") or ""),
        str(getattr(position, "instrument_id", "") or ""),
        str(getattr(position, "strategy_id", "") or ""),
        _enum_name(getattr(position, "side", None)),
        _value_text(getattr(position, "quantity", None)),
        _value_text(getattr(position, "avg_px_open", None)),
        _value_text(getattr(position, "avg_px_close", None)),
        _value_text(getattr(position, "realized_pnl", None)),
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
