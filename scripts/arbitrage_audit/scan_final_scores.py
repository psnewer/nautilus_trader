"""扫描比赛最终比分并补充成交审计工作簿。"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from openpyxl import load_workbook


XLSX = "/Users/miller/Desktop/成交订单比分_start_price_venue_stale_window_20260921-22.xlsx"
LOGS = (
    "/private/tmp/arb_remote_log_20260922.out",
    "/private/tmp/arb_remote_nohup_20260922.out",
)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
LINE = re.compile(
    r"^(2026-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z).*?Sports score updated: "
    r"game=(\d+) (.+?) vs (.+?) score=(.*?) period=([^ ]+) elapsed=.*? status=([^ ]+)$",
)

wb = load_workbook(XLSX, read_only=True, data_only=True)
ws = wb["成交明细"]
targets = {(str(row[2]), str(row[3])) for row in ws.iter_rows(min_row=2, values_only=True)}
latest = {}
for path in LOGS:
    with open(path, "r", errors="replace") as handle:
        for raw in handle:
            if "Sports score updated:" not in raw:
                continue
            match = LINE.search(ANSI.sub("", raw.rstrip("\n")))
            if not match:
                continue
            pair = (match.group(3), match.group(4))
            if pair not in targets:
                continue
            ts = datetime.fromisoformat(match.group(1).replace("Z", "+00:00")).astimezone(timezone.utc)
            score = match.group(5).split("->")[-1]
            if pair not in latest or ts > latest[pair][0]:
                latest[pair] = (ts, score, match.group(6), match.group(7))

for pair in sorted(targets):
    item = latest.get(pair)
    print("|".join((*pair, "-" if item is None else item[1], "-" if item is None else item[2])))
