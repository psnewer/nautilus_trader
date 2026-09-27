"""把单份 nohup.out 的成交订单按统一 bid/start 规则写入 Excel。"""

from __future__ import annotations

import argparse
import ast
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


ANSI = re.compile(r"\x1b\[[0-9;]*m")
ISO_TS = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z)")
BEIJING = timezone(timedelta(hours=8))
SHEET_NAME = "nohup成交_统一规则"
# PMS 比分左右顺序与本地 pair 角色不一致的已核实场次，以实际结算方向为准。
WINNER_OVERRIDES = {
    "Tennis|Julia Grabher|Oksana Selekhmeteva": "no",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    parser.add_argument("audit_json", type=Path)
    parser.add_argument("nohup", type=Path)
    parser.add_argument("output", type=Path)
    return parser.parse_args()


def parse_iso_ts(line: str) -> datetime | None:
    match = ISO_TS.search(line)
    return datetime.fromisoformat(match.group(1).replace("Z", "+00:00")) if match else None


def parse_beijing(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=BEIJING)


def prior(items: list[dict], timestamp: datetime) -> dict | None:
    found = None
    for item in items:
        if item["ts"] <= timestamp:
            found = item
        else:
            break
    return found


def read_log(path: Path) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    snapshots: dict[str, list[dict]] = defaultdict(list)
    scores: dict[str, list[dict]] = defaultdict(list)
    with path.open(errors="replace") as stream:
        for raw in stream:
            line = ANSI.sub("", raw.rstrip("\n"))
            timestamp = parse_iso_ts(line)
            if timestamp is None:
                continue
            if "Strategy evaluate scheduled: pair_id=" in line and "order_books=" in line:
                match = re.search(r"pair_id=(.+?), sport=.*?order_books=(\[.*\])$", line)
                if not match:
                    continue
                try:
                    books = ast.literal_eval(match.group(2))
                except (SyntaxError, ValueError):
                    continue
                snapshots[match.group(1)].append({"ts": timestamp, "books": books})
            elif "Sports score updated: game=" in line:
                match = re.search(
                    r"game=\d+ (.+?) vs (.+?) score=(.*?) period=([^ ]+) elapsed=.*? status=([^ ]+)$",
                    line,
                )
                if match:
                    scores[f"{match.group(1)}|{match.group(2)}"].append({
                        "ts": timestamp,
                        "score": match.group(3).split("->")[-1],
                        "period": match.group(4),
                        "status": match.group(5),
                    })
    return snapshots, scores


def quote_map(books: list[dict]) -> dict[str, dict]:
    return {
        str(item["outcome"]).lower(): {
            "bid": item.get("best_bid"),
            "ask": item.get("best_ask"),
        }
        for item in books
        if str(item.get("venue", "")).upper() == "POLYMARKET"
    }


def tennis_winner(score: str | None) -> tuple[str | None, int, int]:
    """从逗号分隔的盘分推断胜方；当前未结束盘不计入盘胜负。"""
    left_wins = 0
    right_wins = 0
    for raw_set in str(score or "").split(","):
        match = re.match(r"\s*(\d+)\s*-\s*(\d+)", raw_set)
        if not match:
            continue
        left, right = map(int, match.groups())
        high, low = max(left, right), min(left, right)
        if not (high >= 6 and (high - low >= 2 or high == 7)):
            continue
        left_wins += left > right
        right_wins += left < right
    winner = "yes" if left_wins > right_wins else "no" if right_wins > left_wins else None
    return winner, left_wins, right_wins


def main() -> None:
    args = parse_args()
    audit = json.loads(args.audit_json.read_text(encoding="utf-8"))
    source_rows = [row for row in audit["rows"] if row["状态"] in {"已成交", "部分成交"}]
    snapshots, scores = read_log(args.nohup)

    headers = [
        "下单时间(北京时间)", "首次成交时间(北京时间)", "client_order_id", "比赛", "订单状态",
        "规则输入方向(venue_replace前)", "下单时比分", "下单时盘/局", "start_price",
        "start_yes+start_no", "commission区间合格",
        "current_PM_bid(规则判断价)", "current_PM_ask(参考)", "bid/start", "策略动作",
        "规则买入方向", "规则买入价(PM ask)", "实际成交方向", "实际成交价", "成交数量",
        "日志末比分", "日志末状态", "最终输赢", "本金", "毛利润", "手续费", "净利润",
    ]
    rows = []
    for source in source_rows:
        pair = f"{source['赛事']}|{source['选手1']}|{source['选手2']}"
        tail = f"{source['选手1']}|{source['选手2']}"
        order_time = parse_beijing(source["下单时间(北京时间)"])
        if order_time is None:
            continue
        order_time_utc = order_time.astimezone(timezone.utc)
        snapshot = prior(snapshots[pair], order_time_utc)
        quotes = quote_map(snapshot["books"]) if snapshot else {}
        role = str(source.get("venue_replace前方向") or source["下单方向"]).lower()
        role_quote = quotes.get(role, {})
        current_bid = role_quote.get("bid")
        current_ask = role_quote.get("ask")
        start_price = source.get(f"start_{role}")
        start_sum = None
        if source.get("start_yes") is not None and source.get("start_no") is not None:
            start_sum = float(source["start_yes"]) + float(source["start_no"])
        commission_ok = start_sum is not None and 0.98 <= start_sum <= 1.02

        if start_price is None:
            flip = False
            action = "买原方向（缺少start_price，规则不触发）"
        elif not commission_ok:
            flip = False
            action = "买原方向（commission区间不合格）"
        elif current_bid is None:
            flip = False
            action = "买原方向（缺少PM bid，规则不触发）"
        else:
            start_value = float(start_price)
            bid_value = float(current_bid)
            flip = bid_value <= start_value or 1.2 * start_value <= bid_value <= 1.3 * start_value
            action = "买对手盘" if flip else "买原方向"

        buy_role = ("no" if role == "yes" else "yes") if flip else role
        buy_price = quotes.get(buy_role, {}).get("ask")
        latest_score = scores[tail][-1] if scores[tail] else None
        winner_role, left_wins, right_wins = tennis_winner(latest_score["score"] if latest_score else None)
        terminal = latest_score is not None and (
            str(latest_score["status"]).lower() in {"finished", "final", "ended"}
            or max(left_wins, right_wins) >= 2
        )
        if not terminal:
            winner_role = None
        winner_role = WINNER_OVERRIDES.get(pair, winner_role)
        won = winner_role == buy_role if winner_role else None
        quantity = float(source["已成交量"])
        principal = quantity * float(buy_price) if buy_price is not None else None
        gross = quantity * ((1.0 if won else 0.0) - float(buy_price)) if won is not None and buy_price is not None else None
        commission = quantity * float(buy_price) * (1.0 - float(buy_price)) * 0.05 if gross is not None else None

        rows.append([
            order_time.replace(tzinfo=None),
            parse_beijing(source["首次成交时间(北京时间)"]).replace(tzinfo=None),
            source["client_order_id"], f"{source['选手1']} vs {source['选手2']}", source["状态"],
            role, source["下单比分"], source["盘/局"], start_price, start_sum,
            "是" if commission_ok else "否（缺值）" if start_sum is None else "否",
            current_bid, current_ask,
            float(current_bid) / float(start_price) if current_bid is not None and start_price not in {None, 0} else None,
            action, buy_role, buy_price, source["下单方向"], source["成交均价"], quantity,
            latest_score["score"] if latest_score else None,
            latest_score["status"] if latest_score else None,
            "赢" if won else "输" if won is not None else "未出",
            principal, gross, commission, gross - commission if gross is not None else None,
        ])

    workbook = load_workbook(args.workbook)
    if SHEET_NAME in workbook.sheetnames:
        del workbook[SHEET_NAME]
    sheet = workbook.create_sheet(SHEET_NAME, 0)
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    total_row = sheet.max_row + 1
    sheet.cell(total_row, 4, "合计")
    sheet.cell(total_row, 15, f"反买{sum(row[14] == '买对手盘' for row in rows)}笔")
    sheet.cell(total_row, 23, f"{sum(row[22] == '赢' for row in rows)}赢/{sum(row[22] == '输' for row in rows)}输/{sum(row[22] == '未出' for row in rows)}未出")
    for column in (24, 25, 26, 27):
        letter = get_column_letter(column)
        sheet.cell(total_row, column, f"=SUM({letter}2:{letter}{total_row - 1})")

    header_fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for cell in sheet[total_row]:
        cell.fill = PatternFill("solid", fgColor="D9EAF7")
        cell.font = Font(bold=True)
    for row_number in range(2, total_row):
        sheet.cell(row_number, 1).number_format = "yyyy-mm-dd hh:mm:ss.000"
        sheet.cell(row_number, 2).number_format = "yyyy-mm-dd hh:mm:ss.000"
        for column in list(range(9, 15)) + list(range(17, 21)) + list(range(24, 28)):
            sheet.cell(row_number, column).number_format = "0.0000"
    for column in range(24, 28):
        sheet.cell(total_row, column).number_format = "0.0000"
    widths = [23, 23, 17, 43, 13, 24, 24, 13, 13, 18, 19, 27, 21, 12, 38, 13, 21, 15, 13, 12, 24, 14, 12, 13, 13, 13, 13]
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:AA{max(1, total_row - 1)}"
    workbook.save(args.output)
    print(json.dumps({"filled_orders": len(rows), "sheet": SHEET_NAME, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
