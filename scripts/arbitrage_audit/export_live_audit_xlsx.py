"""把 nohup 订单审计 JSON 导出为标准 live 订单 Excel。"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections import defaultdict
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from openpyxl import load_workbook

from src.arbitrage.strategy.consecutive_triggers import same_set
from src.arbitrage.strategy.consecutive_triggers import score_key


ANSI = re.compile(r"\x1b\[[0-9;]*m")
ISO_TS = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z)")
LOCAL_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+)")
BEIJING = timezone(timedelta(hours=8))


def parse_ts(line: str) -> datetime | None:
    match = ISO_TS.search(line)
    if match:
        return datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    match = LOCAL_TS.search(line)
    if match:
        value = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f")
        return value.replace(tzinfo=BEIJING).astimezone(timezone.utc)
    return None


def _tag(line: str, name: str) -> str:
    match = re.search(rf"arb:{name}=([^'\],]+)", line)
    return match.group(1) if match else "unknown"


def log_context(log_path: Path) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    metadata = {}
    triggers: dict[str, list[dict]] = defaultdict(list)
    trigger_pattern = re.compile(
        r"ConsecutiveTriggerGate: pair=(.*?) history=([^ ]+) outcome=(yes|no) "
        r"score='([^']+)' .*?passed=(True|False)",
    )
    with log_path.open(errors="replace") as stream:
        for raw in stream:
            if "OrderInitialized(" not in raw and "ConsecutiveTriggerGate:" not in raw:
                continue
            line = ANSI.sub("", raw)
            ts = parse_ts(line)
            if ts is None:
                continue
            if "OrderInitialized(" in line and "client_order_id=ARB-" in line:
                order_id = re.search(r"client_order_id=(ARB-[^,]+)", line)
                side = re.search(r"side=(BUY|SELL)", line)
                if order_id:
                    metadata[order_id.group(1)] = {
                        "ts": ts,
                        "side": side.group(1) if side else "",
                        "origin": _tag(line, "origin"),
                        "intent": _tag(line, "intent"),
                    }
                continue
            match = trigger_pattern.search(line)
            if match and match.group(2) == "pre_rebate_original_leg" and match.group(5) == "True":
                key = score_key(match.group(4))
                if key is not None:
                    triggers[match.group(1)].append({
                        "ts": ts,
                        "outcome": match.group(3),
                        "score_key": key,
                    })
    return metadata, triggers


def trigger_ratio(row: dict, meta: dict, triggers: dict[str, list[dict]]) -> str | None:
    if meta.get("origin") != "in_game" or meta.get("intent") != "arbitrage":
        return None
    current = score_key(row.get("下单比分") or "")
    if current is None:
        return None
    pair = f"Tennis|{row.get('选手1')}|{row.get('选手2')}"
    counts = {"yes": set(), "no": set()}
    for observation in triggers.get(pair, ()):
        if observation["ts"] > meta["ts"]:
            break
        if same_set(observation["score_key"], current):
            counts[observation["outcome"]].add(observation["score_key"])
    if not counts["yes"] and not counts["no"]:
        return None
    return f"{len(counts['yes'])}:{len(counts['no'])}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("template", type=Path)
    parser.add_argument("audit_json", type=Path)
    parser.add_argument("log", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    rows = json.loads(args.audit_json.read_text(encoding="utf-8"))["rows"]
    first_order_time = {}
    for row in rows:
        pair = (row.get("选手1"), row.get("选手2"))
        order_time = row.get("下单时间(北京时间)") or "9999-12-31 23:59:59.999"
        first_order_time[pair] = min(first_order_time.get(pair, order_time), order_time)
    rows = sorted(rows, key=lambda row: (
        first_order_time[(row.get("选手1"), row.get("选手2"))],
        row.get("下单时间(北京时间)") or "9999-12-31 23:59:59.999",
        row["client_order_id"],
    ))
    metadata, triggers = log_context(args.log)
    workbook = load_workbook(args.template)
    summary = workbook["汇总"]
    detail = workbook["订单明细"]
    by_pair = workbook["按场归集"]
    for sheet in (summary, detail, by_pair):
        sheet.delete_rows(2, max(sheet.max_row - 1, 0))

    detail_headers = [cell.value for cell in detail[1]]
    enriched = []
    for index, row in enumerate(rows, 1):
        meta = metadata.get(row["client_order_id"], {})
        direction = row.get("下单方向")
        values = {
            "序号": index,
            "下单时间(北京时间)": row.get("下单时间(北京时间)"),
            "client_order_id": row.get("client_order_id"),
            "赛事": f"{row.get('选手1')} vs {row.get('选手2')}",
            "选手1": row.get("选手1"),
            "选手2": row.get("选手2"),
            "origin": meta.get("origin", "unknown"),
            "intent": meta.get("intent", "unknown"),
            "side": meta.get("side", ""),
            "下单方向": direction,
            "买入选手": row.get("选手1") if direction == "yes" else row.get("选手2"),
            "限价": row.get("限价"),
            "下单数量": row.get("下单数量"),
            "状态": row.get("状态"),
            "已成交量": row.get("已成交量"),
            "成交均价": row.get("成交均价"),
            "首次成交时间(北京时间)": row.get("首次成交时间(北京时间)"),
            "首次成交比分": row.get("首次成交比分"),
            "下单比分": row.get("下单比分"),
            "触发次数比(YES:NO)": trigger_ratio(row, meta, triggers) if meta else None,
            "盘/局": row.get("盘/局"),
            "撤单时间(北京时间)": row.get("撤单时间(北京时间)"),
            "撤单比分": row.get("撤单比分"),
            "撤单盘/局": row.get("撤单盘/局"),
            "撤单方向PM_bid": row.get("撤单方向PM_bid"),
            "撤单方向PM_ask": row.get("撤单方向PM_ask"),
            "触发venue": row.get("触发venue"),
            "原生腿venue": row.get("原生腿venue"),
            "venue_replace前方向": row.get("venue_replace前方向"),
            "start_yes": row.get("start_yes"),
            "start_no": row.get("start_no"),
        }
        detail.append([values.get(header) for header in detail_headers])
        enriched.append((row, values))

    grouped: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for row, values in enriched:
        grouped[values["赛事"]].append((row, values))
    for pair, items in sorted(grouped.items()):
        statuses = Counter(row.get("状态") for row, _ in items)
        origins = Counter(values["origin"] for _, values in items)
        intents = Counter(values["intent"] for _, values in items)
        by_pair.append([
            pair,
            len(items),
            sum(float(row.get("已成交量") or 0) > 0 for row, _ in items),
            sum(float(row.get("已成交量") or 0) for row, _ in items),
            statuses["未成交-风控拒绝"],
            statuses["未成交-已撤单"],
            sum("无终态" in str(row.get("状态")) for row, _ in items),
            ", ".join(f"{key}:{value}" for key, value in sorted(origins.items())),
            ", ".join(f"{key}:{value}" for key, value in sorted(intents.items())),
        ])

    statuses = Counter(row.get("状态") for row in rows)
    origins = Counter(values["origin"] for _, values in enriched)
    intents = Counter(values["intent"] for _, values in enriched)
    times = [row["下单时间(北京时间)"] for row in rows if row.get("下单时间(北京时间)")]
    summary_rows = [
        ("模式", "LIVE 实盘"),
        ("下单总数", len(rows)),
        ("涉及比赛数", len(grouped)),
        *[(f"  状态:{key}", value) for key, value in sorted(statuses.items())],
        ("成交量合计", sum(float(row.get("已成交量") or 0) for row in rows)),
        *[(f"  origin:{key}", value) for key, value in sorted(origins.items())],
        *[(f"  intent:{key}", value) for key, value in sorted(intents.items())],
        ("时间范围", f"{min(times)} ~ {max(times)}" if times else None),
        ("数据源", "本地下载的远端 live nohup.out（日记重建），非 paper SQLite"),
    ]
    for item in summary_rows:
        summary.append(item)

    for sheet in (summary, detail, by_pair):
        sheet.auto_filter.ref = sheet.dimensions
    workbook.save(args.output)
    print(json.dumps({
        "output": str(args.output),
        "orders": len(rows),
        "pairs": len(grouped),
        "filled_orders": sum(float(row.get("已成交量") or 0) > 0 for row in rows),
        "fill_qty": sum(float(row.get("已成交量") or 0) for row in rows),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
