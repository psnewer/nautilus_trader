"""按进程启动时间从累计 paper Excel 提取单次运行明细。"""

from __future__ import annotations

import argparse
from collections import Counter
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment
from openpyxl.styles import Font
from openpyxl.styles import PatternFill


HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(color="FFFFFF", bold=True)


def _style_header(sheet) -> None:
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def filter_workbook(source: Path, output: Path, since: datetime) -> tuple[int, int]:
    workbook = load_workbook(source)
    detail = workbook["订单明细"]
    headers = [cell.value for cell in detail[1]]
    index = {str(value): offset for offset, value in enumerate(headers)}

    kept = []
    for values in detail.iter_rows(min_row=2, values_only=True):
        submitted = values[index["提交时间(北京时间)"]]
        if isinstance(submitted, datetime) and submitted >= since:
            kept.append(values)

    detail.delete_rows(2, max(detail.max_row - 1, 0))
    for values in kept:
        detail.append(values)
    detail.auto_filter.ref = detail.dimensions

    for name in ("Pair汇总", "类型汇总", "说明", "利润汇总"):
        if name in workbook.sheetnames:
            del workbook[name]

    grouped: dict[str, list[tuple]] = defaultdict(list)
    for row in kept:
        grouped[str(row[index["pair_id"]] or "(metadata缺失)")].append(row)
    pair_sheet = workbook.create_sheet("Pair汇总")
    pair_sheet.append([
        "pair_id", "赛事", "选手1", "选手2", "订单数", "成交数", "LIVE数", "撤单数",
        "BUY数", "SELL数", "成交数量", "成交名义额", "origin", "intent", "首次提交", "最后提交",
    ])
    for pair_id, rows in sorted(grouped.items()):
        statuses = Counter(row[index["status"]] for row in rows)
        pair_sheet.append([
            pair_id,
            rows[0][index["赛事"]],
            rows[0][index["选手1"]],
            rows[0][index["选手2"]],
            len(rows),
            statuses["MATCHED"],
            statuses["LIVE"],
            statuses["CANCELED"],
            sum(row[index["side"]] == "BUY" for row in rows),
            sum(row[index["side"]] == "SELL" for row in rows),
            sum(float(row[index["fill_qty"]] or 0) for row in rows),
            sum(float(row[index["fill_notional"]] or 0) for row in rows),
            ", ".join(sorted({str(row[index["origin"]] or "unknown") for row in rows})),
            ", ".join(sorted({str(row[index["intent"]] or "unknown") for row in rows})),
            min(row[index["提交时间(北京时间)"]] for row in rows),
            max(row[index["提交时间(北京时间)"]] for row in rows),
        ])
    _style_header(pair_sheet)

    by_type: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for row in kept:
        by_type[(str(row[index["origin"]] or "unknown"), str(row[index["intent"]] or "unknown"))].append(row)
    type_sheet = workbook.create_sheet("类型汇总")
    type_sheet.append([
        "origin", "intent", "订单数", "成交数", "LIVE数", "撤单数", "成交数量", "成交名义额",
    ])
    for (origin, intent), rows in sorted(by_type.items()):
        statuses = Counter(row[index["status"]] for row in rows)
        type_sheet.append([
            origin,
            intent,
            len(rows),
            statuses["MATCHED"],
            statuses["LIVE"],
            statuses["CANCELED"],
            sum(float(row[index["fill_qty"]] or 0) for row in rows),
            sum(float(row[index["fill_notional"]] or 0) for row in rows),
        ])
    _style_header(type_sheet)

    statuses = Counter(row[index["status"]] for row in kept)
    profit_sheet = workbook.create_sheet("利润汇总")
    profit_sheet.append(["项目", "数值"])
    profit_sheet.append(["运行起点(北京时间)", since])
    profit_sheet.append(["订单数", len(kept)])
    profit_sheet.append(["比赛数", len(grouped)])
    profit_sheet.append(["成交订单数", statuses["MATCHED"]])
    profit_sheet.append(["LIVE订单数", statuses["LIVE"]])
    profit_sheet.append(["撤单数", statuses["CANCELED"]])
    profit_sheet.append(["实际利润", 0 if statuses["MATCHED"] == 0 else None])
    profit_sheet.append(["利润口径", "只统计本轮新建订单；上一轮遗留订单及其本轮成交全部排除。"])
    _style_header(profit_sheet)

    notes = workbook.create_sheet("说明")
    notes.append(["项目", "说明"])
    notes.append(["源文件", str(source.resolve())])
    notes.append(["运行起点", since])
    notes.append(["筛选口径", "提交时间不早于运行起点；上一轮遗留订单全部排除。"])
    notes.append(["成交口径", "只有本轮新建订单的成交才计入；reconciliation 恢复的旧成交不计。"])
    notes.append(["利润", "本轮新建订单无成交，因此无需赛果即可确定实际利润为 0。"])
    _style_header(notes)

    output.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output)
    return len(kept), statuses["MATCHED"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--since", required=True, help="北京时间，例如 2026-10-15T23:46:38")
    args = parser.parse_args()
    orders, matched = filter_workbook(args.source, args.output, datetime.fromisoformat(args.since))
    print(f"saved={args.output} orders={orders} matched={matched}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
