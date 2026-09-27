"""从单个 nohup.out 重建全部 ARB 下单时数据及未成交单最接近限价的 OBD。"""

from __future__ import annotations

import argparse
import ast
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


ANSI = re.compile(r"\x1b\[[0-9;]*m")
ISO_TS = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z)")
LOCAL_TS = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})")
UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))
VENUES = ("POLYMARKET", "ORBITEXCH", "SHARPEXCH")
ROLES = ("yes", "no")


def parse_ts(line: str) -> datetime | None:
    match = ISO_TS.search(line)
    if match:
        return datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    match = LOCAL_TS.search(line)
    if match:
        local = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f")
        return local.replace(tzinfo=BEIJING).astimezone(UTC)
    return None


def last_before(items: list[dict], ts: datetime, max_age: float | None = None) -> dict | None:
    found = None
    for item in items:
        if item["ts"] <= ts:
            found = item
        else:
            break
    if found is not None and max_age is not None:
        if (ts - found["ts"]).total_seconds() > max_age:
            return None
    return found


def book_map(books: list[dict]) -> dict[tuple[str, str], dict]:
    return {
        (str(item["venue"]).upper(), str(item["outcome"]).lower()): {
            "bid": item.get("best_bid"),
            "ask": item.get("best_ask"),
        }
        for item in books
    }


def ask_vector(books: dict[tuple[str, str], dict]) -> list[float | None]:
    return [books.get((venue, role), {}).get("ask") for venue in VENUES for role in ROLES]


def candidate_matches(books: dict[tuple[str, str], dict], rate: float) -> list[dict]:
    by_role: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (venue, role), quote in books.items():
        ask = quote.get("ask")
        if role in ROLES and ask is not None and float(ask) > 0:
            by_role[role].append((venue, float(ask)))
    matches = []
    for yes_venue, yes_ask in by_role["yes"]:
        for no_venue, no_ask in by_role["no"]:
            if yes_venue == no_venue:
                continue
            total = yes_ask + no_ask
            for target, denominator in (("yes", yes_ask), ("no", no_ask)):
                calculated = (1.0 - total) / denominator
                if abs(calculated - rate) < 2e-7:
                    matches.append({
                        "target": target,
                        "legs": [(yes_venue, "yes"), (no_venue, "no")],
                    })
    return matches


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    snapshots: dict[str, list[dict]] = defaultdict(list)
    scores: dict[str, list[dict]] = defaultdict(list)
    starts: dict[str, list[dict]] = defaultdict(list)
    preparations: dict[str, list[dict]] = defaultdict(list)
    drops: dict[str, list[dict]] = defaultdict(list)
    orders: dict[str, dict] = {}
    fills: dict[str, list[dict]] = defaultdict(list)
    terminal: dict[str, dict] = {}

    with args.input.open("r", errors="replace") as stream:
        for line_number, raw in enumerate(stream, 1):
            line = ANSI.sub("", raw.rstrip("\n"))
            ts = parse_ts(line)
            if ts is None:
                continue

            if "Strategy evaluate scheduled: pair_id=" in line and "order_books=" in line:
                match = re.search(
                    r"Strategy evaluate scheduled: pair_id=(.+?), sport=.*?, event=([^,]+), order_books=(\[.*\])$",
                    line,
                )
                if match:
                    try:
                        books = ast.literal_eval(match.group(3))
                    except (SyntaxError, ValueError):
                        continue
                    snapshots[match.group(1)].append({
                        "ts": ts,
                        "event": match.group(2),
                        "books": books,
                        "skipped": False,
                    })
                continue

            if "Strategy evaluate skipped: pair_id=" in line and "reason=pair_" in line:
                match = re.search(r"Strategy evaluate skipped: pair_id=(.+?), reason=pair_", line)
                if match and snapshots[match.group(1)]:
                    snapshots[match.group(1)][-1]["skipped"] = True
                continue

            if "Sports score updated: game=" in line:
                match = re.search(
                    r"game=(\d+) (.+?) vs (.+?) score=(.*?) period=([^ ]+) elapsed=.*? status=([^ ]+)$",
                    line,
                )
                if match:
                    pair_tail = f"{match.group(2)}|{match.group(3)}"
                    scores[pair_tail].append({
                        "ts": ts,
                        "score": match.group(4).split("->")[-1],
                        "period": match.group(5),
                    })
                continue

            if "Start price captured: pair=" in line:
                match = re.search(
                    r"Start price captured: pair=(.+?) game=\d+ source=([^ ]+) prices=(\{.*\})$",
                    line,
                )
                if match:
                    try:
                        prices = ast.literal_eval(match.group(3))
                    except (SyntaxError, ValueError):
                        continue
                    starts[match.group(1)].append({
                        "ts": ts,
                        "source": match.group(2),
                        "yes": prices.get("yes"),
                        "no": prices.get("no"),
                    })
                continue

            if "PlaceBets[prepare]: pair=" in line:
                match = re.search(r"pair=(.+?) legs=\d+ strategy=([^ ]+) rate=([^ ]+)", line)
                if match:
                    preparations[match.group(1)].append({
                        "ts": ts,
                        "strategy": match.group(2),
                        "rate": float(match.group(3)),
                    })
                continue

            if " drop leg=" in line and "pair=" in line:
                match = re.search(
                    r"pair=(.+?) drop leg=.*?\.(POLYMARKET|ORBITEXCH|SHARPEXCH).*? outcome=(yes|no)",
                    line,
                )
                if match:
                    drops[match.group(1)].append({
                        "ts": ts,
                        "venue": match.group(2),
                        "role": match.group(3),
                    })
                continue

            if "OrderInitialized(" in line and "client_order_id=ARB-" in line:
                order_id = re.search(r"client_order_id=(ARB-[^,]+)", line)
                pair = re.search(r"arb:pair_id=([^']+)", line)
                role = re.search(r"arb:leg_key=[^:]+:([^:']+):", line)
                price = re.search(r"options=\{'price': '([^']+)'", line)
                quantity = re.search(r"quantity=([0-9.]+)", line)
                if order_id and pair and role and price and quantity:
                    orders[order_id.group(1)] = {
                        "id": order_id.group(1),
                        "ts": ts,
                        "pair": pair.group(1),
                        "role": role.group(1),
                        "price": float(price.group(1)),
                        "quantity": float(quantity.group(1)),
                        "line": line_number,
                    }
                continue

            if "OrderFilled(" in line and "client_order_id=ARB-" in line:
                order_id = re.search(r"client_order_id=(ARB-[^,]+)", line)
                quantity = re.search(r"last_qty=([0-9.]+)", line)
                price = re.search(r"last_px=([0-9.]+)", line)
                if order_id and quantity and price:
                    fills[order_id.group(1)].append({
                        "ts": ts,
                        "quantity": float(quantity.group(1)),
                        "price": float(price.group(1)),
                    })
                continue

            if "client_order_id=ARB-" in line:
                event = re.search(r"<--\[EVT\] (Order(?:Canceled|Rejected|Denied|Expired))\(", line)
                order_id = re.search(r"client_order_id=(ARB-[^,]+)", line)
                if event and order_id:
                    terminal[order_id.group(1)] = {"ts": ts, "event": event.group(1)}

    for values in (snapshots, scores, starts, preparations, drops):
        for items in values.values():
            items.sort(key=lambda item: item["ts"])

    rows = []
    for order in sorted(orders.values(), key=lambda item: item["ts"]):
        pair = order["pair"]
        sport, player_1, player_2 = pair.split("|", 2)
        pair_snapshots = snapshots[pair]
        prep = last_before(preparations[pair], order["ts"], 5)
        anchor = prep["ts"] if prep else order["ts"]
        eligible = [item for item in pair_snapshots if not item["skipped"]]
        trigger = last_before(eligible, anchor, 5)
        trigger_index = pair_snapshots.index(trigger) if trigger in pair_snapshots else -1
        previous = pair_snapshots[trigger_index - 1] if trigger_index > 0 else None
        trigger_books = book_map(trigger["books"]) if trigger else {}
        previous_books = book_map(previous["books"]) if previous else {}

        changed_venues = sorted({
            venue
            for venue in VENUES
            if any(
                trigger_books.get((venue, role), {}).get("ask")
                != previous_books.get((venue, role), {}).get("ask")
                for role in ROLES
            )
        })
        other_change = None
        other_venues = sorted(set(VENUES) - set(changed_venues)) if len(changed_venues) == 1 else []
        if trigger_index >= 0:
            for later in pair_snapshots[trigger_index + 1:]:
                later_books = book_map(later["books"])
                changed_other = [
                    venue
                    for venue in other_venues
                    if any(
                        later_books.get((venue, role), {}).get("ask")
                        != trigger_books.get((venue, role), {}).get("ask")
                        for role in ROLES
                    )
                ]
                if changed_other:
                    other_change = {
                        "ts": later["ts"],
                        "venues": changed_other,
                        "books": later_books,
                    }
                    break

        score = last_before(scores[f"{player_1}|{player_2}"], order["ts"])
        start = last_before(starts[pair], order["ts"])

        native_venue = None
        native_role = None
        if prep and trigger:
            candidates = candidate_matches(trigger_books, prep["rate"])
            recent_drops = [
                (item["venue"], item["role"])
                for item in drops[pair]
                if 0 <= (order["ts"] - item["ts"]).total_seconds() <= 2
            ]
            native_candidates = set()
            for candidate in candidates:
                remaining = [leg for leg in candidate["legs"] if leg not in recent_drops]
                if len(remaining) == 1:
                    native_candidates.add(remaining[0])
            if len(native_candidates) == 1:
                native_venue, native_role = next(iter(native_candidates))
            else:
                role_venues = {
                    venue
                    for candidate in candidates
                    for venue, role in candidate["legs"]
                    if role == order["role"]
                }
                if len(role_venues) == 1:
                    native_venue = next(iter(role_venues))

        order_fills = fills.get(order["id"], [])
        filled_quantity = sum(item["quantity"] for item in order_fills)
        average_fill_price = (
            sum(item["quantity"] * item["price"] for item in order_fills) / filled_quantity
            if filled_quantity > 0 else None
        )
        end = terminal.get(order["id"])
        if filled_quantity >= order["quantity"] - 1e-6:
            status = "已成交"
        elif filled_quantity > 0:
            status = "部分成交"
        elif end:
            status = {
                "OrderCanceled": "未成交-已撤单",
                "OrderRejected": "未成交-拒单",
                "OrderDenied": "未成交-风控拒绝",
                "OrderExpired": "未成交-已过期",
            }[end["event"]]
        else:
            status = "未成交-日志结束时无终态"

        closest = None
        if status != "已成交":
            cutoff = end["ts"] if end else datetime.max.replace(tzinfo=UTC)
            for snapshot in pair_snapshots:
                if snapshot["ts"] <= order["ts"] or snapshot["ts"] > cutoff:
                    continue
                if "Deltas" not in snapshot["event"]:
                    continue
                books = book_map(snapshot["books"])
                quote = books.get(("POLYMARKET", order["role"]), {})
                ask = quote.get("ask")
                if ask is None:
                    continue
                distance = abs(float(ask) - order["price"])
                if closest is None or (distance, snapshot["ts"]) < (closest["distance"], closest["ts"]):
                    closest = {
                        "ts": snapshot["ts"],
                        "distance": distance,
                        "bid": quote.get("bid"),
                        "ask": float(ask),
                        "books": books,
                    }

        row = {
            "下单时间(北京时间)": order["ts"].astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "client_order_id": order["id"],
            "状态": status,
            "赛事": sport,
            "选手1": player_1,
            "选手2": player_2,
            "下单方向": order["role"],
            "限价": order["price"],
            "下单数量": order["quantity"],
            "已成交量": filled_quantity,
            "首次成交时间(北京时间)": (
                order_fills[0]["ts"].astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                if order_fills else None
            ),
            "成交均价": average_fill_price,
            "下单比分": score["score"] if score else None,
            "盘/局": score["period"] if score else None,
            "start_yes": start["yes"] if start else None,
            "start_no": start["no"] if start else None,
            "start来源": start["source"] if start else None,
            "原生腿venue": native_venue,
            "venue_replace前方向": native_role,
            "触发venue": ",".join(changed_venues) or None,
            **dict(zip(
                [f"变化前_{venue}_{role.upper()}" for venue in ("PM", "OE", "SE") for role in ROLES],
                ask_vector(previous_books),
                strict=True,
            )),
            **dict(zip(
                [f"触发时_{venue}_{role.upper()}" for venue in ("PM", "OE", "SE") for role in ROLES],
                ask_vector(trigger_books),
                strict=True,
            )),
            "另一venue变化间隔(秒)": (
                (other_change["ts"] - trigger["ts"]).total_seconds()
                if other_change and trigger else None
            ),
            "后来变化venue": ",".join(other_change["venues"]) if other_change else None,
            **dict(zip(
                [f"变化后_{venue}_{role.upper()}" for venue in ("PM", "OE", "SE") for role in ROLES],
                ask_vector(other_change["books"]) if other_change else [None] * 6,
                strict=True,
            )),
            "最接近限价OBD时间(北京时间)": (
                closest["ts"].astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                if closest else None
            ),
            "最接近OBD距下单(秒)": (
                (closest["ts"] - order["ts"]).total_seconds() if closest else None
            ),
            "最接近OBD_方向PM_bid": closest["bid"] if closest else None,
            "最接近OBD_方向PM_ask": closest["ask"] if closest else None,
            "最接近OBD_ask减限价": closest["ask"] - order["price"] if closest else None,
            **dict(zip(
                [f"最接近OBD_{venue}_{role.upper()}_ask" for venue in ("PM", "OE", "SE") for role in ROLES],
                ask_vector(closest["books"]) if closest else [None] * 6,
                strict=True,
            )),
        }
        rows.append(row)

    result = {
        "source": str(args.input),
        "order_count": len(rows),
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "rows": rows,
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "order_count": len(rows),
        "filled": sum(row["状态"] == "已成交" for row in rows),
        "unfilled_or_partial": sum(row["状态"] != "已成交" for row in rows),
        "output": str(args.output),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
