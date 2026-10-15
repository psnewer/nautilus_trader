"""用旧版 NT 日志为 paper SQLite 回填 pair/origin/intent 等审计字段。"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.arbitrage.paper.polymarket import PaperStore


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
ORDER_RE = re.compile(
    r"OrderInitialized\(instrument_id=([^,]+), client_order_id=([^,]+),.*?tags=(\[.*\])\)",
)
SESSION_RE = re.compile(
    r"Execution session accepted: client_order_id=([^,]+), venue_order_id=([^,]+), instrument_id=([^;]+);",
)
TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z")


def _tag_values(tags: list[str]) -> dict[str, str]:
    result = {}
    for tag in tags:
        if tag.startswith("arb:") and "=" in tag:
            key, value = tag[4:].split("=", 1)
            result[key] = value
    return result


def _timestamp_ns(line: str) -> int | None:
    match = TIMESTAMP_RE.match(line)
    if match is None:
        return None
    seconds = int(datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc,
    ).timestamp())
    fraction = (match.group(2) or "")[:9].ljust(9, "0")
    return seconds * 1_000_000_000 + int(fraction or 0)


def enrich(database: Path, log_path: Path) -> tuple[int, int, int]:
    by_client: dict[str, dict] = {}
    venue_to_client: dict[str, str] = {}
    with log_path.open("r", encoding="utf-8", errors="replace") as source:
        for raw_line in source:
            line = ANSI_RE.sub("", raw_line)
            order_match = ORDER_RE.search(line)
            if order_match is not None:
                tags = ast.literal_eval(order_match.group(3))
                values = _tag_values(tags)
                leg_key = values.get("leg_key", "")
                leg_parts = leg_key.split(":")
                by_client[order_match.group(2)] = {
                    "client_order_id": order_match.group(2),
                    "pair_id": values.get("pair_id"),
                    "opportunity_id": values.get("opportunity_id"),
                    "leg_key": leg_key or None,
                    "intent": values.get("intent"),
                    "origin": values.get("origin"),
                    "instrument_id": order_match.group(1),
                    "selection_role": leg_parts[1] if len(leg_parts) > 1 else None,
                    "order_score": values.get("order_score"),
                    "ts_submitted_ns": _timestamp_ns(line),
                }
            session_match = SESSION_RE.search(line)
            if session_match is not None:
                venue_to_client[session_match.group(2)] = session_match.group(1)

    store = PaperStore(str(database), 0.0)
    updated = 0
    try:
        orders = store.orders()
        for order in orders:
            client_order_id = venue_to_client.get(str(order["id"]))
            metadata = by_client.get(client_order_id or "")
            if metadata is not None and store.update_order_metadata(str(order["id"]), metadata):
                updated += 1
    finally:
        store.close()
    return len(orders), updated, len(by_client)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args(argv)
    total, updated, initialized = enrich(args.database, args.log)
    print(f"orders={total} updated={updated} initialized_in_log={initialized}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
