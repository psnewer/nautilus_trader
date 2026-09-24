"""从套利运行日志重建成交、比分、start_price 与 stale-window 报告。"""

import ast
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from itertools import product


FILES = [
    "/private/tmp/arb_remote_log_20260922.out",
    "/private/tmp/arb_remote_nohup_20260922.out",
]
ANSI = re.compile(r"\x1b\[[0-9;]*m")
ISO_TS = re.compile(r"(2026-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z)")
LOCAL_TS = re.compile(r"^(2026-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})")


def parse_ts(line):
    m = ISO_TS.search(line)
    if m:
        return datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
    m = LOCAL_TS.search(line)
    if m:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f")
        return dt.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
    return None


snapshots = defaultdict(list)
scores = defaultdict(list)
phases = defaultdict(list)
matches = []
subscriptions = []
preps = defaultdict(list)
legs = defaultdict(list)
inits = {}
fills = []

for path in FILES:
    recent_match = None
    recent_match_ts = None
    with open(path, "r", errors="replace") as f:
        for lineno, raw in enumerate(f, 1):
            line = ANSI.sub("", raw.rstrip("\n"))
            ts = parse_ts(line)
            if ts is None:
                continue

            if "MarketMatchingActor: MatchedPair " in line:
                m = re.search(r"MatchedPair (.+?) \(conf=", line)
                if m:
                    recent_match = m.group(1)
                    recent_match_ts = ts
                    matches.append((ts, recent_match, path))

            if "SportsGameUpdate{'game_id':" in line and "SubscribeData" in line:
                m = re.search(r"game_id': (\d+)", line)
                if m and recent_match and recent_match_ts and (ts - recent_match_ts).total_seconds() < 2:
                    subscriptions.append((ts, recent_match, int(m.group(1))))

            if "Strategy evaluate scheduled: pair_id=" in line and "order_books=" in line:
                m = re.search(
                    r"Strategy evaluate scheduled: pair_id=(.+?), sport=.*?, event=([^,]+), order_books=(\[.*\])$",
                    line,
                )
                if m:
                    try:
                        books = ast.literal_eval(m.group(3))
                    except Exception:
                        continue
                    snapshots[m.group(1)].append({
                        "ts": ts,
                        "event": m.group(2),
                        "books": books,
                        "skipped": False,
                        "path": path,
                        "line": lineno,
                    })

            if "Strategy evaluate skipped: pair_id=" in line and "reason=pair_" in line:
                m = re.search(r"Strategy evaluate skipped: pair_id=(.+?), reason=pair_", line)
                if m and snapshots[m.group(1)]:
                    snapshots[m.group(1)][-1]["skipped"] = True

            if "Sports score updated: game=" in line:
                m = re.search(
                    r"game=(\d+) (.+?) vs (.+?) score=(.*?) period=([^ ]+) elapsed=.*? status=([^ ]+)$",
                    line,
                )
                if m:
                    pair_tail = f"{m.group(2)}|{m.group(3)}"
                    scores[pair_tail].append({
                        "ts": ts,
                        "game": int(m.group(1)),
                        "transition": m.group(4),
                        "score": m.group(4).split("->")[-1],
                        "period": m.group(5),
                        "status": m.group(6),
                    })

            if "Sports phase updated: game=" in line:
                m = re.search(r"game=(\d+) phase=([^ ]+) source=([^:]+):", line)
                if m:
                    phases[int(m.group(1))].append({"ts": ts, "phase": m.group(2), "source": m.group(3)})

            if "PlaceBets[prepare]: pair=" in line:
                m = re.search(r"pair=(.+?) legs=(\d+) strategy=([^ ]+) rate=([^ ]+)", line)
                if m:
                    preps[m.group(1)].append({
                        "ts": ts,
                        "legs": int(m.group(2)),
                        "strategy": m.group(3),
                        "rate": float(m.group(4)),
                    })

            if "PlaceBets leg: pair=" in line:
                m = re.search(r"pair=(.+?) venue=([^ ]+) role=([^ ]+) price=([^ ]+) qty=([^ ]+)", line)
                if m:
                    legs[m.group(1)].append({
                        "ts": ts,
                        "venue": m.group(2),
                        "role": m.group(3),
                        "price": float(m.group(4)),
                        "qty": float(m.group(5)),
                    })

            if "OrderInitialized(" in line and "client_order_id=ARB-" in line:
                mid = re.search(r"client_order_id=(ARB-[^,]+)", line)
                mpair = re.search(r"arb:pair_id=([^']+)", line)
                miid = re.search(r"instrument_id=([^,]+)", line)
                mprice = re.search(r"options=\{'price': '([^']+)'", line)
                mrole = re.search(r"arb:leg_key=[^:]+:([^:']+):", line)
                if mid and mpair:
                    inits[mid.group(1)] = {
                        "ts": ts,
                        "pair": mpair.group(1),
                        "instrument": miid.group(1) if miid else None,
                        "price": float(mprice.group(1)) if mprice else None,
                        "role": mrole.group(1) if mrole else None,
                    }

            if "OrderFilled(" in line and "client_order_id=ARB-" in line:
                mid = re.search(r"client_order_id=(ARB-[^,]+)", line)
                mpx = re.search(r"last_px=([0-9.]+)", line)
                mqty = re.search(r"last_qty=([0-9.]+)", line)
                mcomm = re.search(r"commission=([0-9.]+)", line)
                if mid:
                    fills.append({
                        "ts": ts,
                        "id": mid.group(1),
                        "px": float(mpx.group(1)),
                        "qty": float(mqty.group(1)),
                        "commission": float(mcomm.group(1)),
                        "path": path,
                        "line": lineno,
                    })


def book_map(snapshot):
    return {
        (str(x["venue"]).upper(), str(x["outcome"]).lower()): {
            "bid": x.get("best_bid"),
            "ask": x.get("best_ask"),
        }
        for x in snapshot["books"]
    }


def last_before(items, ts, max_age=None):
    found = None
    for item in items:
        if item["ts"] <= ts:
            found = item
        else:
            break
    if found is not None and max_age is not None and (ts - found["ts"]).total_seconds() > max_age:
        return None
    return found


def candidate_matches(books, rate):
    by_role = defaultdict(list)
    for (venue, role), quote in books.items():
        ask = quote.get("ask")
        if ask is not None and ask > 0 and role in ("yes", "no"):
            by_role[role].append((venue, float(ask)))
    out = []
    for yes, no in product(by_role["yes"], by_role["no"]):
        if yes[0] == no[0]:
            continue
        total = yes[1] + no[1]
        combo = {"yes": yes, "no": no}
        for target in ("yes", "no"):
            calc = (1.0 - total) / combo[target][1]
            if abs(calc - rate) < 2e-7:
                out.append({
                    "target": target,
                    "rate": calc,
                    "combo": {k: {"venue": v[0], "ask": v[1]} for k, v in combo.items()},
                })
    return out


reports = []
for fill in sorted(fills, key=lambda x: x["ts"]):
    init = inits.get(fill["id"])
    if not init:
        continue
    pair = init["pair"]
    prep = last_before(preps[pair], init["ts"], 5)
    leg = last_before(legs[pair], init["ts"], 5)
    snap_list = sorted(snapshots[pair], key=lambda x: x["ts"])
    trigger_anchor = prep["ts"] if prep else init["ts"]
    trigger = last_before([s for s in snap_list if not s["skipped"]], trigger_anchor, 5)
    trigger_idx = snap_list.index(trigger) if trigger else -1
    prev = snap_list[trigger_idx - 1] if trigger_idx > 0 else None
    tb = book_map(trigger) if trigger else {}
    pb = book_map(prev) if prev else {}
    changed = sorted({venue for venue, _ in tb if any(
        tb.get((venue, role), {}).get("ask") != pb.get((venue, role), {}).get("ask")
        for role in ("yes", "no")
    )})
    rate = prep["rate"] if prep else None
    candidates = candidate_matches(tb, rate) if rate is not None else []
    native_venues = sorted({c["combo"][init["role"]]["venue"] for c in candidates if init["role"] in c["combo"]})

    # 找触发 venue 之外最先发生 ask 变化的快照。
    other_change = None
    other_venues = sorted({v for v, _ in tb} - set(changed)) if len(changed) == 1 else []
    for later in snap_list[trigger_idx + 1:] if trigger_idx >= 0 else []:
        lb = book_map(later)
        changed_other = [v for v in other_venues if any(
            lb.get((v, role), {}).get("ask") != tb.get((v, role), {}).get("ask")
            for role in ("yes", "no")
        )]
        if changed_other:
            other_change = {
                "ts": later["ts"],
                "venues": changed_other,
                "books": lb,
                "delay_s": (later["ts"] - trigger["ts"]).total_seconds(),
            }
            break

    pair_tail = "|".join(pair.split("|")[-2:])
    score = last_before(sorted(scores[pair_tail], key=lambda x: x["ts"]), fill["ts"])
    pair_scores = sorted(scores[pair_tail], key=lambda x: x["ts"])
    game = score["game"] if score else (pair_scores[0]["game"] if pair_scores else None)
    fill_path = fill["path"]
    match_times = sorted(
        x[0] for x in matches if x[1] == pair and x[2] == fill_path and x[0] <= fill["ts"]
    )
    matched_at = match_times[0] if match_times else (snap_list[0]["ts"] if snap_list else None)
    inplay_events = []
    if game is not None:
        inplay_events.extend(
            x["ts"] for x in phases.get(game, [])
            if x["phase"] == "IN_PLAY" and x["ts"] <= fill["ts"]
        )
        inplay_events.extend(
            x["ts"] for x in pair_scores
            if x["status"] == "inprogress" and x["ts"] <= fill["ts"]
        )
    inplay_at = min(inplay_events) if inplay_events else None
    pre_events = [] if game is None else [
        x["ts"] for x in phases.get(game, [])
        if x["phase"] == "PRE"
        and (inplay_at is None or x["ts"] < inplay_at)
    ]
    pre_obd_witness = False
    pre_pm_top_change_witness = False
    pre_obd_count = 0
    pre_complete_count = 0
    if pre_events and inplay_at is not None:
        pre_from = max(min(pre_events), matched_at) if matched_at is not None else min(pre_events)
        for idx, s in enumerate(snap_list):
            if not (
                s["event"] in ("MarketOrderBookDeltas", "OrderBookDeltas")
                and pre_from <= s["ts"] < inplay_at
            ):
                continue
            pre_obd_count += 1
            bm = book_map(s)
            yes = bm.get(("POLYMARKET", "yes"), {}).get("ask")
            no = bm.get(("POLYMARKET", "no"), {}).get("ask")
            if yes is None or no is None or yes <= 0 or no <= 0:
                continue
            pre_complete_count += 1
            if 0.95 <= yes + no <= 1.05:
                pre_obd_witness = True
                previous = book_map(snap_list[idx - 1]) if idx > 0 else {}
                if any(
                    bm.get(("POLYMARKET", role), {}).get(side)
                    != previous.get(("POLYMARKET", role), {}).get(side)
                    for role in ("yes", "no")
                    for side in ("bid", "ask")
                ):
                    pre_pm_top_change_witness = True
    start_snapshot = last_before(snap_list, inplay_at) if inplay_at else None
    start_books = book_map(start_snapshot) if start_snapshot else {}
    if pre_obd_witness and start_snapshot is not None:
        start_prices = {
            role: start_books.get(("POLYMARKET", role), {}).get("ask")
            for role in ("yes", "no")
        }
        start_source = "captured" if pre_pm_top_change_witness else "possible_captured"
    else:
        start_prices = {"yes": 0.6, "no": 0.6}
        start_source = "default"
    if start_source == "captured":
        start_reason = "qualified_pre_witness"
    elif start_source == "possible_captured":
        start_reason = "pm_delta_origin_not_provable_from_log"
    elif matched_at is None or inplay_at is None:
        start_reason = "missing_match_or_inplay_anchor"
    elif matched_at >= inplay_at:
        start_reason = "late_join_after_inplay"
    elif not pre_events:
        start_reason = "no_pre_phase_observed"
    elif pre_obd_count == 0:
        start_reason = "no_pm_obd_during_pre"
    elif pre_complete_count == 0:
        start_reason = "incomplete_pm_vector_during_pre"
    else:
        start_reason = "pm_vector_sum_outside_0.95_1.05"
    effective = (fill["px"] * fill["qty"] + fill["commission"]) / fill["qty"]
    reports.append({
        "id": fill["id"],
        "pair": pair,
        "fill_ts": fill["ts"].isoformat(),
        "role": init["role"],
        "fill_px": fill["px"],
        "effective_px": effective,
        "start_prices": start_prices,
        "start_price": start_prices.get(init["role"]),
        "start_source": start_source,
        "start_reason": start_reason,
        "pre_pm_top_change_witness": pre_pm_top_change_witness,
        "start_ts": inplay_at.isoformat() if inplay_at else None,
        "score": score,
        "rate": rate,
        "trigger_ts": trigger["ts"].isoformat() if trigger else None,
        "trigger_books": {f"{k[0]}:{k[1]}": v for k, v in tb.items()},
        "previous_ts": prev["ts"].isoformat() if prev else None,
        "previous_books": {f"{k[0]}:{k[1]}": v for k, v in pb.items()},
        "trigger_changed_venues": changed,
        "candidate_matches": candidates,
        "native_venues_for_filled_role": native_venues,
        "other_change": None if other_change is None else {
            **{k: v for k, v in other_change.items() if k != "books"},
            "ts": other_change["ts"].isoformat(),
            "books": {f"{k[0]}:{k[1]}": v for k, v in other_change["books"].items()},
        },
    })

def asks(d):
    def value(venue, role):
        item = d.get(f"{venue}:{role}")
        return None if item is None else item["ask"]
    return "/".join(
        "-" if x is None else f"{x:.6f}".rstrip("0").rstrip(".")
        for x in (
            value("POLYMARKET", "yes"), value("POLYMARKET", "no"),
            value("ORBITEXCH", "yes"), value("ORBITEXCH", "no"),
            value("SHARPEXCH", "yes"), value("SHARPEXCH", "no"),
        )
    )


if len(sys.argv) > 1 and sys.argv[1] == "trigger-pm-bids":
    for r in reports:
        print(json.dumps({
            "fill_ts": r["fill_ts"],
            "pair": r["pair"],
            "pm_yes_bid": r["trigger_books"].get("POLYMARKET:yes", {}).get("bid"),
            "pm_no_bid": r["trigger_books"].get("POLYMARKET:no", {}).get("bid"),
        }, ensure_ascii=False))
    raise SystemExit


if len(sys.argv) > 1 and sys.argv[1] == "bid-profit":
    from openpyxl import load_workbook

    workbook = load_workbook(
        "/Users/miller/Desktop/成交订单比分_start_price_venue_stale_window_20260921-22.xlsx",
        read_only=True,
        data_only=True,
    )
    source_sheet = workbook["成交明细"]
    native_sheet = workbook["原生PM腿利润测算"]
    source_headers = {cell.value: cell.column for cell in source_sheet[1]}
    native_rows = list(native_sheet.iter_rows(min_row=2, values_only=True))
    calculated = []
    for index, (report, native_row) in enumerate(zip(reports, native_rows, strict=True), 2):
        native_role = native_row[4]
        native_outcome = native_row[5]
        winner = native_row[6]
        opponent_role = "no" if native_role == "yes" else "yes"
        native_bid = report["trigger_books"][f"POLYMARKET:{native_role}"]["bid"]
        opponent_bid = report["trigger_books"][f"POLYMARKET:{opponent_role}"]["bid"]
        venue = source_sheet.cell(index, source_headers["原生腿venue"]).value
        quantity = 5.05
        calculated.append({
            "venue": venue,
            "native_profit": (quantity if native_outcome == winner else 0.0) - quantity * native_bid,
            "opponent_profit": (quantity if native_outcome != winner else 0.0) - quantity * opponent_bid,
            "native_win": native_outcome == winner,
            "opponent_win": native_outcome != winner,
            "native_principal": quantity * native_bid,
            "opponent_principal": quantity * opponent_bid,
        })

    def summarize(items, prefix):
        for direction in ("native", "opponent"):
            principal = sum(item[f"{direction}_principal"] for item in items)
            profit = sum(item[f"{direction}_profit"] for item in items)
            print(json.dumps({
                "scope": prefix,
                "direction": direction,
                "orders": len(items),
                "wins": sum(item[f"{direction}_win"] for item in items),
                "principal": round(principal, 6),
                "payout": round(sum(item[f"{direction}_win"] for item in items) * 5.05, 6),
                "commission": 0.0,
                "profit": round(profit, 6),
                "roi": round(profit / principal, 8),
            }, ensure_ascii=False))

    summarize([item for item in calculated if item["venue"] == "ORBITEXCH"], "cross_venue_45")
    summarize(calculated, "all_49")
    raise SystemExit


if len(sys.argv) > 1 and sys.argv[1] in (
    "bid-fillability",
    "bid-minus-005-fillability",
    "limit-minus-005-fillability",
):
    from openpyxl import load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    use_limit_price = sys.argv[1] == "limit-minus-005-fillability"
    price_offset = 0.05 if sys.argv[1] != "bid-fillability" else 0.0
    if use_limit_price:
        sheet_prefix = "limit减0.05挂单成交性"
    else:
        sheet_prefix = "bid减0.05挂单成交性" if price_offset else "bid挂单成交性"
    report_path = "/Users/miller/Desktop/成交订单比分_start_price_venue_stale_window_20260921-22.xlsx"
    workbook = load_workbook(report_path)
    source_sheet = workbook["成交明细"]
    native_sheet = workbook["原生PM腿利润测算"]
    source_headers = {cell.value: cell.column for cell in source_sheet[1]}
    native_rows = list(native_sheet.iter_rows(min_row=2, values_only=True))
    calculated = []

    for index, (report, native_row) in enumerate(zip(reports, native_rows, strict=True), 2):
        if source_sheet.cell(index, source_headers["原生腿venue"]).value != "ORBITEXCH":
            continue
        native_role = native_row[4]
        native_outcome = native_row[5]
        winner = native_row[6]
        opponent_role = "no" if native_role == "yes" else "yes"
        trigger_ts = datetime.fromisoformat(report["trigger_ts"])
        pair_snapshots = sorted(snapshots[report["pair"]], key=lambda item: item["ts"])

        direction_results = {}
        for direction, role, outcome in (
            ("原始PM腿", native_role, native_outcome),
            ("对手PM腿", opponent_role, native_row[3] if native_outcome == native_row[2] else native_row[2]),
        ):
            market_bid = float(report["trigger_books"][f"POLYMARKET:{role}"]["bid"])
            if use_limit_price:
                other_role = "no" if role == "yes" else "yes"
                other_ask = float(report["trigger_books"][f"POLYMARKET:{other_role}"]["ask"])
                limit_base = min(market_bid, 1.0 - other_ask)
            else:
                other_ask = None
                limit_base = market_bid
            bid = round(limit_base - price_offset, 6)
            future_asks = []
            crossed_at = None
            crossed_ask = None
            for snapshot in pair_snapshots:
                if snapshot["ts"] <= trigger_ts:
                    continue
                ask = book_map(snapshot).get(("POLYMARKET", role), {}).get("ask")
                if ask is None:
                    continue
                future_asks.append(float(ask))
                if crossed_at is None and float(ask) <= float(bid):
                    crossed_at = snapshot["ts"]
                    crossed_ask = float(ask)
            confirmed = crossed_at is not None
            quantity = 5.05
            profit = ((quantity if outcome == winner else 0.0) - quantity * float(bid)) if confirmed else 0.0
            direction_results[direction] = {
                "role": role,
                "outcome": outcome,
                "market_bid": market_bid,
                "other_ask": other_ask,
                "limit_base": limit_base,
                "bid": float(bid),
                "min_future_ask": min(future_asks) if future_asks else None,
                "status": "可确认成交" if confirmed else "未确认成交",
                "crossed_at": crossed_at,
                "crossed_ask": crossed_ask,
                "delay_s": (crossed_at - trigger_ts).total_seconds() if crossed_at else None,
                "won": outcome == winner if confirmed else None,
                "profit": profit,
            }

        calculated.append({
            "trigger_ts": trigger_ts,
            "sport": native_row[1],
            "player_1": native_row[2],
            "player_2": native_row[3],
            "winner": winner,
            **direction_results,
        })

    for sheet_name in (f"{sheet_prefix}汇总", f"{sheet_prefix}明细"):
        if sheet_name in workbook.sheetnames:
            del workbook[sheet_name]

    summary_sheet = workbook.create_sheet(f"{sheet_prefix}汇总", 0)
    summary_sheet.append(["方向", "尝试数", "可确认成交", "未确认成交", "成交后赢", "成交后输", "零手续费利润", "成交本金", "收益率"])
    summaries = []
    for direction in ("原始PM腿", "对手PM腿"):
        items = [item[direction] for item in calculated]
        filled = [item for item in items if item["status"] == "可确认成交"]
        principal = sum(5.05 * item["bid"] for item in filled)
        profit = sum(item["profit"] for item in filled)
        summary = [
            direction,
            len(items),
            len(filled),
            len(items) - len(filled),
            sum(item["won"] is True for item in filled),
            sum(item["won"] is False for item in filled),
            profit,
            principal,
            profit / principal if principal else None,
        ]
        summaries.append(summary)
        summary_sheet.append(summary)
    for cell in summary_sheet[1]:
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.font = Font(color="FFFFFF", bold=True)
    for row_index in range(2, summary_sheet.max_row + 1):
        summary_sheet.cell(row_index, 7).number_format = "0.000000"
        summary_sheet.cell(row_index, 8).number_format = "0.000000"
        summary_sheet.cell(row_index, 9).number_format = "0.00%"
    for column in range(1, summary_sheet.max_column + 1):
        summary_sheet.column_dimensions[get_column_letter(column)].width = 16

    detail_sheet = workbook.create_sheet(f"{sheet_prefix}明细", 1)
    detail_headers = ["触发时间(UTC)", "赛事", "选手1", "选手2", "最终胜者"]
    for direction in ("原始PM腿", "对手PM腿"):
        detail_headers.extend([
            f"{direction}_outcome", f"{direction}_挂单价", f"{direction}_后续最低ask",
            f"{direction}_状态", f"{direction}_首次穿价时间UTC", f"{direction}_穿价ask",
            f"{direction}_穿价延迟秒", f"{direction}_结果", f"{direction}_利润",
        ])
    detail_sheet.append(detail_headers)
    for item in calculated:
        row = [item["trigger_ts"].replace(tzinfo=None), item["sport"], item["player_1"], item["player_2"], item["winner"]]
        for direction in ("原始PM腿", "对手PM腿"):
            result = item[direction]
            row.extend([
                result["outcome"], result["bid"], result["min_future_ask"], result["status"],
                result["crossed_at"].replace(tzinfo=None) if result["crossed_at"] else None,
                result["crossed_ask"], result["delay_s"],
                "赢" if result["won"] is True else "输" if result["won"] is False else None,
                result["profit"],
            ])
        detail_sheet.append(row)
    for cell in detail_sheet[1]:
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    for row_index in range(2, detail_sheet.max_row + 1):
        detail_sheet.cell(row_index, 1).number_format = "yyyy-mm-dd hh:mm:ss"
        for status_column in (9, 18):
            cell = detail_sheet.cell(row_index, status_column)
            cell.fill = PatternFill("solid", fgColor="D9EAD3" if cell.value == "可确认成交" else "FFF2CC")
        for time_column in (10, 19):
            detail_sheet.cell(row_index, time_column).number_format = "yyyy-mm-dd hh:mm:ss"
    detail_sheet.freeze_panes = "A2"
    detail_sheet.auto_filter.ref = detail_sheet.dimensions
    for column in range(1, detail_sheet.max_column + 1):
        detail_sheet.column_dimensions[get_column_letter(column)].width = 18
    for column in (3, 4, 5, 6, 15):
        detail_sheet.column_dimensions[get_column_letter(column)].width = 23

    workbook.save(report_path)
    for summary in summaries:
        output = dict(zip(
            ("direction", "attempts", "confirmed_fills", "unconfirmed", "wins", "losses", "profit", "principal", "roi"),
            summary,
            strict=True,
        ))
        output["price_offset"] = price_offset
        print(json.dumps(output, ensure_ascii=False))
    raise SystemExit


if len(sys.argv) > 1 and sys.argv[1] == "diagnose-defaults":
    print("COUNT_DEFAULT\t" + str(sum(r["start_source"] == "default" for r in reports)))
    print("COUNT_QUALIFIED_BY_PM_TOP_CHANGE\t" + str(sum(r["pre_pm_top_change_witness"] for r in reports)))
    for r in reports:
        if r["start_source"] == "default":
            print("|".join([
                r["pair"],
                r["fill_ts"],
                r["start_reason"],
                r["score"]["score"] if r["score"] else "-",
            ]))
    for r in reports:
        if r["start_source"] == "possible_captured":
            print("UNCERTAIN|" + r["pair"] + "|" + r["fill_ts"])
    raise SystemExit

print("COUNT\t" + str(len(reports)))
print("NOTE\tcaptured=日志可确认PRE期间PM顶价变化; possible_captured=有合格PRE快照但日志未记录delta来源; default=开赛后才配对，0.6/0.6仅占位")
print("time|pair|role|fill/effective|score(period)|start(y/n;source)|native|trigger|prev=>at|delay/other|after")
for r in reports:
    local = datetime.fromisoformat(r["fill_ts"]).astimezone(timezone(timedelta(hours=8)))
    score_text = "-" if not r["score"] else f'{r["score"]["score"]}({r["score"]["period"]})'
    start = r["start_prices"]
    other = r["other_change"]
    print("|".join([
        local.strftime("%m-%d %H:%M:%S"),
        r["pair"],
        r["role"],
        f'{r["fill_px"]:.3f}/{r["effective_px"]:.3f}',
        score_text,
        f'{start["yes"]:.3f}/{start["no"]:.3f};{r["start_source"]}',
        ",".join(r["native_venues_for_filled_role"]) or "?",
        ",".join(r["trigger_changed_venues"]) or "?",
        asks(r["previous_books"]) + "=>" + asks(r["trigger_books"]),
        ("-" if not other else f'{other["delay_s"]:.3f}s/{",".join(other["venues"])}'),
        "-" if not other else asks(other["books"]),
    ]))
