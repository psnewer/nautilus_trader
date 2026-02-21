"""
生成执行服务的合成事件流（JSONL）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class Event:
    ts_ms: int
    session_id: str
    type: str
    payload: dict[str, Any]


def _emit(events: list[Event], ts_ms: int, session_id: str, event_type: str, payload: dict[str, Any]) -> int:
    events.append(Event(ts_ms=ts_ms, session_id=session_id, type=event_type, payload=payload))
    return ts_ms + 50


def _scenario_place_success(session_id: str, ts_ms: int) -> list[Event]:
    events: list[Event] = []
    ts_ms = _emit(events, ts_ms, session_id, "session_start", {"scenario": "place_success"})
    ts_ms = _emit(events, ts_ms, session_id, "plan_generated", {
        "plan_id": "plan-1",
        "operations": [
            {"op_id": "op-1", "type": "place", "venue": "polymarket", "market_type": "home"},
        ],
    })
    ts_ms = _emit(events, ts_ms, session_id, "operation_submitted", {"op_id": "op-1"})
    ts_ms = _emit(events, ts_ms, session_id, "operation_feedback", {"op_id": "op-1", "success": True})
    ts_ms = _emit(events, ts_ms, session_id, "tracking_update", {
        "op_id": "op-1",
        "status": "confirmed",
        "size_matched": 10.0,
        "size_remaining": 0.0,
    })
    _emit(events, ts_ms, session_id, "session_end", {"reason": "target_met"})
    return events


def _scenario_place_fail(session_id: str, ts_ms: int) -> list[Event]:
    events: list[Event] = []
    ts_ms = _emit(events, ts_ms, session_id, "session_start", {"scenario": "place_failed"})
    ts_ms = _emit(events, ts_ms, session_id, "plan_generated", {
        "plan_id": "plan-2",
        "operations": [
            {"op_id": "op-2", "type": "place", "venue": "orbitexch", "market_type": "away"},
        ],
    })
    ts_ms = _emit(events, ts_ms, session_id, "operation_submitted", {"op_id": "op-2"})
    ts_ms = _emit(events, ts_ms, session_id, "operation_feedback", {
        "op_id": "op-2",
        "success": False,
        "message": "rejected",
    })
    _emit(events, ts_ms, session_id, "session_end", {"reason": "max_failure_retries"})
    return events


def _scenario_cancel_success(session_id: str, ts_ms: int) -> list[Event]:
    events: list[Event] = []
    ts_ms = _emit(events, ts_ms, session_id, "session_start", {"scenario": "cancel_success"})
    ts_ms = _emit(events, ts_ms, session_id, "plan_generated", {
        "plan_id": "plan-3",
        "operations": [
            {"op_id": "op-3", "type": "cancel", "venue": "orbitexch", "market_type": "draw"},
        ],
    })
    ts_ms = _emit(events, ts_ms, session_id, "operation_submitted", {"op_id": "op-3"})
    ts_ms = _emit(events, ts_ms, session_id, "tracking_timeout", {"plan_id": "plan-3"})
    ts_ms = _emit(events, ts_ms, session_id, "refresh_result", {
        "op_id": "op-3",
        "status": "confirmed",
    })
    _emit(events, ts_ms, session_id, "session_end", {"reason": "target_met"})
    return events


def _scenario_cancel_fail(session_id: str, ts_ms: int) -> list[Event]:
    events: list[Event] = []
    ts_ms = _emit(events, ts_ms, session_id, "session_start", {"scenario": "cancel_failed"})
    ts_ms = _emit(events, ts_ms, session_id, "plan_generated", {
        "plan_id": "plan-4",
        "operations": [
            {"op_id": "op-4", "type": "cancel", "venue": "polymarket", "market_type": "home"},
        ],
    })
    ts_ms = _emit(events, ts_ms, session_id, "operation_submitted", {"op_id": "op-4"})
    ts_ms = _emit(events, ts_ms, session_id, "tracking_timeout", {"plan_id": "plan-4"})
    ts_ms = _emit(events, ts_ms, session_id, "refresh_result", {
        "op_id": "op-4",
        "status": "failed",
        "message": "order_still_exists",
    })
    _emit(events, ts_ms, session_id, "session_end", {"reason": "max_failure_retries"})
    return events


def _scenario_modify_success(session_id: str, ts_ms: int) -> list[Event]:
    events: list[Event] = []
    ts_ms = _emit(events, ts_ms, session_id, "session_start", {"scenario": "modify_success"})
    ts_ms = _emit(events, ts_ms, session_id, "plan_generated", {
        "plan_id": "plan-5",
        "operations": [
            {"op_id": "op-5", "type": "modify", "venue": "orbitexch", "market_type": "home"},
        ],
    })
    ts_ms = _emit(events, ts_ms, session_id, "operation_submitted", {"op_id": "op-5"})
    ts_ms = _emit(events, ts_ms, session_id, "tracking_timeout", {"plan_id": "plan-5"})
    ts_ms = _emit(events, ts_ms, session_id, "refresh_result", {
        "op_id": "op-5",
        "status": "confirmed",
        "size_matched": 2.0,
    })
    _emit(events, ts_ms, session_id, "session_end", {"reason": "target_met"})
    return events


def _scenario_modify_fail(session_id: str, ts_ms: int) -> list[Event]:
    events: list[Event] = []
    ts_ms = _emit(events, ts_ms, session_id, "session_start", {"scenario": "modify_failed"})
    ts_ms = _emit(events, ts_ms, session_id, "plan_generated", {
        "plan_id": "plan-6",
        "operations": [
            {"op_id": "op-6", "type": "modify", "venue": "orbitexch", "market_type": "away"},
        ],
    })
    ts_ms = _emit(events, ts_ms, session_id, "operation_submitted", {"op_id": "op-6"})
    ts_ms = _emit(events, ts_ms, session_id, "tracking_timeout", {"plan_id": "plan-6"})
    ts_ms = _emit(events, ts_ms, session_id, "refresh_result", {
        "op_id": "op-6",
        "status": "failed",
        "message": "no_fill_update",
    })
    _emit(events, ts_ms, session_id, "session_end", {"reason": "max_failure_retries"})
    return events


def _scenario_multi_recovery(session_id: str, ts_ms: int) -> list[Event]:
    events: list[Event] = []
    ts_ms = _emit(events, ts_ms, session_id, "session_start", {"scenario": "multi_recovery"})
    ts_ms = _emit(events, ts_ms, session_id, "plan_generated", {
        "plan_id": "plan-7",
        "operations": [
            {"op_id": "op-7", "type": "place", "venue": "polymarket", "market_type": "home"},
        ],
    })
    ts_ms = _emit(events, ts_ms, session_id, "operation_submitted", {"op_id": "op-7"})
    ts_ms = _emit(events, ts_ms, session_id, "operation_feedback", {"op_id": "op-7", "success": True})
    ts_ms = _emit(events, ts_ms, session_id, "tracking_update", {
        "op_id": "op-7",
        "status": "confirmed",
        "size_matched": 5.0,
        "size_remaining": 5.0,
    })
    ts_ms = _emit(events, ts_ms, session_id, "plan_generated", {
        "plan_id": "plan-8",
        "operations": [
            {"op_id": "op-8", "type": "cancel", "venue": "polymarket", "market_type": "home"},
        ],
    })
    ts_ms = _emit(events, ts_ms, session_id, "operation_submitted", {"op_id": "op-8"})
    ts_ms = _emit(events, ts_ms, session_id, "tracking_timeout", {"plan_id": "plan-8"})
    ts_ms = _emit(events, ts_ms, session_id, "refresh_result", {
        "op_id": "op-8",
        "status": "confirmed",
    })
    ts_ms = _emit(events, ts_ms, session_id, "plan_generated", {
        "plan_id": "plan-9",
        "operations": [
            {"op_id": "op-9", "type": "modify", "venue": "orbitexch", "market_type": "home"},
        ],
    })
    ts_ms = _emit(events, ts_ms, session_id, "operation_submitted", {"op_id": "op-9"})
    ts_ms = _emit(events, ts_ms, session_id, "operation_feedback", {"op_id": "op-9", "success": True})
    ts_ms = _emit(events, ts_ms, session_id, "tracking_update", {
        "op_id": "op-9",
        "status": "confirmed",
        "size_matched": 5.0,
        "size_remaining": 0.0,
    })
    _emit(events, ts_ms, session_id, "session_end", {"reason": "target_met"})
    return events


def _scenario_failure_accumulate(session_id: str, ts_ms: int) -> list[Event]:
    events: list[Event] = []
    ts_ms = _emit(events, ts_ms, session_id, "session_start", {"scenario": "failure_accumulate"})
    for idx in range(3):
        plan_id = f"plan-f{idx + 1}"
        op_id = f"op-f{idx + 1}"
        ts_ms = _emit(events, ts_ms, session_id, "plan_generated", {
            "plan_id": plan_id,
            "operations": [
                {"op_id": op_id, "type": "place", "venue": "orbitexch", "market_type": "away"},
            ],
        })
        ts_ms = _emit(events, ts_ms, session_id, "operation_submitted", {"op_id": op_id})
        ts_ms = _emit(events, ts_ms, session_id, "operation_feedback", {
            "op_id": op_id,
            "success": False,
            "message": "rejected",
            "failure_count": idx + 1,
        })
        ts_ms = _emit(events, ts_ms, session_id, "failure_count_update", {"count": idx + 1})
    _emit(events, ts_ms, session_id, "session_end", {"reason": "max_failure_retries"})
    return events


def generate_streams() -> list[Event]:
    events: list[Event] = []
    ts_ms = 1_700_000_000_000
    for idx, scenario in enumerate(
        (
            _scenario_place_success,
            _scenario_place_fail,
            _scenario_cancel_success,
            _scenario_cancel_fail,
            _scenario_modify_success,
            _scenario_modify_fail,
            _scenario_multi_recovery,
            _scenario_failure_accumulate,
        ),
        start=1,
    ):
        session_id = f"session-{idx:02d}"
        events.extend(scenario(session_id, ts_ms))
        ts_ms += 1_000
    return events


def main() -> None:
    output_path = Path(__file__).with_name("execution_event_stream.jsonl")
    events = generate_streams()
    with output_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(asdict(event), ensure_ascii=True))
            handle.write("\n")


if __name__ == "__main__":
    main()
