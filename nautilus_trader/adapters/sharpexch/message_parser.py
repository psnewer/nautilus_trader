"""SharpExch WebSocket 消息解析器。

解析 competition prices WS 与 execution general WS 帧;DataClient/ExecutionClient 共用。
"""

from __future__ import annotations

import json
import logging
from typing import Any


class SharpExchMessageParser:
    """解析 SharpExch `multiple-market-prices` / `general` 帧。"""

    def __init__(self) -> None:
        self._log = logging.getLogger(self.__class__.__name__)

    def parse_price_message(self, message: dict) -> dict[str, Any] | None:
        """解析 BIAB/OE 型 price frame。

        支持 `bdatb`/`bdatl` 以及常见 `batb`/`batl` 兼容字段。
        """

        try:
            market_id = message.get("id")
            if not market_id:
                return None
            market_def = message.get("marketDefinition") or {}
            runners = []
            for runner in message.get("rc") or []:
                if not isinstance(runner, dict):
                    continue
                selection_id = runner.get("id")
                if selection_id is None:
                    continue
                runners.append(
                    {
                        "selection_id": str(selection_id),
                        "back": _levels(runner.get("bdatb") or runner.get("batb") or []),
                        "lay": _levels(runner.get("bdatl") or runner.get("batl") or []),
                        "total_volume": _to_float(runner.get("tv"), 0.0),
                        "locked": bool(runner.get("locked", False)),
                    },
                )
            return {
                "market_id": str(market_id),
                "event_id": message.get("mainEventId"),
                "event_name": message.get("mainEventName", "Unknown"),
                "market_name": message.get("marketNameWithParents", "Unknown"),
                "status": market_def.get("status", "UNKNOWN"),
                "in_play": bool(market_def.get("inPlay", False)),
                "runners": runners,
                "timestamp": message.get("apiPt"),
            }
        except Exception as exc:
            self._log.error("解析 SharpExch 赔率消息失败: %s", exc)
            return None

    def parse_general_frame(self, message: dict) -> dict[str, Any] | None:
        """解析 SE `general` 频道帧。

        当前只做类型拆分和基础数值解析,不做 USD/venue 币种换算。
        """

        if not isinstance(message, dict):
            return None

        if "BALANCE" in message:
            payload = _decode_nested_json(message.get("BALANCE")) or {}
            if not isinstance(payload, dict):
                self._log.debug("未知 SharpExch BALANCE payload,忽略: %s", str(payload)[:120])
                return None
            return {
                "type": "balance",
                "balance": _to_optional_float(payload.get("balance")),
                "av_balance": _to_optional_float(payload.get("avBalance")),
            }

        if "CURRENT_BETS" in message:
            bets = _decode_nested_json(message.get("CURRENT_BETS")) or []
            if not isinstance(bets, list):
                self._log.debug("未知 SharpExch CURRENT_BETS payload,忽略: %s", str(bets)[:120])
                return None
            return {
                "type": "current_bets",
                "bets": [bet for bet in bets if isinstance(bet, dict)],
            }

        self._log.debug("未知 SharpExch general 帧,忽略: %s", str(message)[:120])
        return None

    def parse_order_message(self, message: dict) -> dict[str, Any] | None:
        """兼容 OE adapter 的旧命名;SE 后续 execution client 可直接调 general parser。"""

        return self.parse_general_frame(message)

    @staticmethod
    def get_runner_by_selection_id(parsed_message: dict, selection_id: str) -> dict | None:
        for runner in parsed_message.get("runners", []):
            if runner.get("selection_id") == selection_id:
                return runner
        return None


def _levels(raw_levels) -> list[dict[str, float]]:
    levels: list[dict[str, float]] = []
    if isinstance(raw_levels, dict):
        raw_levels = raw_levels.values()
    for item in raw_levels:
        if isinstance(item, dict):
            price = _to_float(item.get("odds"), 0.0)
            size = _to_float(item.get("amount"), 0.0)
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            price = _to_float(item[1], 0.0)
            size = _to_float(item[2], 0.0)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price = _to_float(item[0], 0.0)
            size = _to_float(item[1], 0.0)
        else:
            continue
        levels.append({"price": price, "size": size})
    return levels


def _decode_nested_json(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _to_optional_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
