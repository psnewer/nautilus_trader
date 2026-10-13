"""
用当前配置和真实策略树逐帧回放 nohup 日志。

该工具只做离线审计,不连接远端、不下单。它重建日志中的 OBD、比分、
pair_in_flight 跳帧和策略运行时内存,并在 PlaceBets 产出计划后应用可由日志
确定重建的概率 Risk 门控。余额、profit gate 与真实异步提交时序无法从日志
完整还原,输出会明确称为“模拟接受”,而不是实盘成交。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import gzip
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from launchers.arb_node import register_builtin_checks_and_actions
from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.strategy.checks.quote_legs import best_probabilities_by_outcome
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.condition import evaluate_tree
from src.arbitrage.strategy.consecutive_triggers import ConsecutiveTriggerStore
from src.arbitrage.strategy.json_loader import build_strategy_registry


ANSI = re.compile(r"\x1b\[[0-9;]*m")
ISO_TS = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)Z")
LOCAL_TS = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3})")
SCORE_RE = re.compile(
    r"Sports score updated: game=(\d+) (.+?) vs (.+?) score=(.*?) "
    r"period=([^ ]+) elapsed=.*? status=([^ ]+)",
)
SCHEDULE_RE = re.compile(
    r"evaluate scheduled: pair_id=(Tennis\|.+?), sport=.*?, "
    r"event=([^,]+), order_books=(\[.*\])$",
)
SKIP_RE = re.compile(r"evaluate skipped: pair_id=(Tennis\|.+?), reason=pair_in_flight")
INSTRUMENT_IDS = {
    ("POLYMARKET", "yes"): "pmyes.POLYMARKET",
    ("POLYMARKET", "no"): "pmno.POLYMARKET",
    ("ORBITEXCH", "yes"): "oeyes.ORBITEXCH",
    ("ORBITEXCH", "no"): "oeno.ORBITEXCH",
    ("SHARPEXCH", "yes"): "seyes.SHARPEXCH",
    ("SHARPEXCH", "no"): "seno.SHARPEXCH",
}


def _open_text(path: Path):
    return (
        gzip.open(path, "rt", errors="replace")
        if path.suffix == ".gz"
        else path.open(errors="replace")
    )


def _timestamp_ns(line: str) -> int | None:
    match = ISO_TS.search(line)
    if match:
        return int(datetime.fromisoformat(match.group(1) + "+00:00").timestamp() * 1_000_000_000)
    match = LOCAL_TS.search(line)
    if match:
        value = datetime.strptime(".".join(match.groups()), "%Y-%m-%d %H:%M:%S.%f").replace(
            tzinfo=timezone(timedelta(hours=8)),
        )
        return int(value.timestamp() * 1_000_000_000)
    return None


def _winner_from_score(score: str) -> str | None:
    yes_sets = no_sets = 0
    for token in str(score or "").split(","):
        match = re.match(r"\s*(\d+)\s*-\s*(\d+)", token)
        if match is None:
            continue
        left, right = map(int, match.groups())
        yes_sets += left > right
        no_sets += right > left
    if yes_sets >= 2 and yes_sets > no_sets:
        return "yes"
    if no_sets >= 2 and no_sets > yes_sets:
        return "no"
    return None


class _Instrument:
    def __init__(self, instrument_id: str, venue: str, outcome: str, competition: str):
        self.id = instrument_id
        self.info = {
            "claim": outcome,
            "selection_role": "",
            "quote_claim": "yes",
            "venue": venue,
            "competition": competition,
        }
        if venue in {"ORBITEXCH", "SHARPEXCH"} and outcome == "no":
            self.info["exec_instrument_id"] = INSTRUMENT_IDS[(venue, "yes")]
        self.min_quantity = None
        self.max_quantity = None
        self.size_precision = 2
        self.price_precision = 2


class _Book:
    def __init__(self, bid, ask):
        self._bid = bid
        self._ask = ask

    def best_bid_price(self):
        return self._bid

    def best_ask_price(self):
        return self._ask


class _Cache:
    def __init__(self, competition: str):
        self._competition = competition
        self._general = {}
        self._books = {}
        self._instruments = {}

    def set_books(self, books):
        self._books = {}
        for item in books:
            venue = str(item["venue"]).upper()
            outcome = str(item["outcome"]).lower()
            instrument_id = INSTRUMENT_IDS.get((venue, outcome))
            if instrument_id is None:
                continue
            self._instruments.setdefault(
                instrument_id,
                _Instrument(instrument_id, venue, outcome, self._competition),
            )
            self._books[instrument_id] = _Book(item.get("best_bid"), item.get("best_ask"))

    def instrument(self, instrument_id):
        return self._instruments.get(str(instrument_id))

    def order_book(self, instrument_id):
        return self._books.get(str(instrument_id))

    def positions_open(self, instrument_id=None):
        return []

    def orders_open(self, *args, **kwargs):
        return []

    def get(self, key):
        return self._general.get(key)

    def add(self, key, value):
        self._general[key] = value

    def delete(self, key):
        self._general.pop(key, None)


class _Registry:
    def __init__(self, pair_id: str, game_id: int):
        self._pair_id = pair_id
        self._game_id = game_id

    def instrument_ids_for_pair(self, pair_id):
        return list(INSTRUMENT_IDS.values())

    def get(self, instrument_id):
        return self._pair_id

    def game_id_for_pair(self, pair_id):
        return self._game_id


class _SportsState:
    def __init__(self, score: str, period: str, status: str):
        self.score = score
        self.period = period
        self.status = status
        self.live = status == "inprogress"
        self.ended = status == "finished"
        self.source = "REPLAY"
        self.phase = "IN_PLAY" if self.live else ("ENDED" if self.ended else "PRE")


class _SportsStore:
    def __init__(self):
        self._states = {}

    def set(self, game_id, state):
        self._states[game_id] = state

    def get(self, game_id):
        return self._states.get(game_id)


class _Portfolio:
    def __init__(self):
        self._shares = {}

    def add_fill(self, venue, role, share):
        key = (str(venue).lower(), str(role))
        self._shares[key] = self._shares.get(key, 0.0) + float(share)

    def outcome_shares(self, pair_id, venue):
        result = {}
        for (stored_venue, role), share in self._shares.items():
            if venue is None or stored_venue == str(venue).lower():
                result[role] = result.get(role, 0.0) + share
        return result

    def outcome_shares_for_venue(self, pair_id, venue, claim):
        return {
            role: share
            for (stored_venue, role), share in self._shares.items()
            if stored_venue == str(venue).lower()
        }


def _parse_log(path: Path, selected_pairs: set[str]):  # noqa: C901
    evaluations = defaultdict(dict)
    scores = defaultdict(list)
    game_ids = {}
    skip_times = defaultdict(list)
    with _open_text(path) as stream:
        for raw in stream:
            line = ANSI.sub("", raw.rstrip())
            timestamp = _timestamp_ns(line)
            if timestamp is None:
                continue
            score_match = SCORE_RE.search(line)
            if score_match:
                pair = f"Tennis|{score_match.group(2)}|{score_match.group(3)}"
                if not selected_pairs or pair in selected_pairs:
                    game_ids[pair] = int(score_match.group(1))
                    scores[pair].append(
                        (
                            timestamp,
                            score_match.group(4).split("->")[-1].strip(),
                            score_match.group(5),
                            score_match.group(6),
                        ),
                    )
                continue
            schedule_match = SCHEDULE_RE.search(line)
            if schedule_match:
                pair = schedule_match.group(1)
                if not selected_pairs or pair in selected_pairs:
                    evaluations[pair][timestamp] = (
                        schedule_match.group(2),
                        schedule_match.group(3),
                    )
                continue
            skip_match = SKIP_RE.search(line)
            if skip_match and (not selected_pairs or skip_match.group(1) in selected_pairs):
                skip_times[skip_match.group(1)].append(timestamp)
    for pair in scores:
        scores[pair] = sorted({row[0]: row for row in scores[pair]}.values())
    skipped = defaultdict(set)
    for pair, times in skip_times.items():
        scheduled = sorted(evaluations.get(pair, {}))
        for skipped_at in sorted(times):
            candidates = [
                value
                for value in scheduled
                if value <= skipped_at and skipped_at - value <= 2_000_000_000
            ]
            if candidates:
                skipped[pair].add(candidates[-1])
    return evaluations, scores, game_ids, skipped


def _score_at(scores, timestamp):
    current = None
    for row in scores:
        if row[0] <= timestamp:
            current = row[1:]
        else:
            break
    return current or ("", "", "")


def _probability_risk_allows(legs, risk_config: dict) -> bool:
    minimum = float(risk_config.get("min_probability", 0.0))
    maximum = float(risk_config.get("max_probability", 1.0))
    buy_only = bool(risk_config.get("prob_buy_only", False))
    for leg in legs:
        side = str(leg.get("side", "BUY")).upper()
        if buy_only and side != "BUY":
            continue
        try:
            probability = float(leg["price"])
        except (KeyError, TypeError, ValueError):
            return False
        if not minimum <= probability <= maximum:
            return False
    return True


async def _replay_pair(pair, game_id, pair_evaluations, pair_scores, skipped, config, competition):
    cache = _Cache(competition)
    registry = _Registry(pair, game_id)
    sports = _SportsStore()
    portfolio = _Portfolio()
    trigger_store = ConsecutiveTriggerStore()
    price_store = PairPriceStore(cache)
    price_store.initialize(pair, ("yes", "no"))
    strategy_registry = build_strategy_registry(
        type(
            "Settings",
            (),
            {
                "strategies": config["strategy"]["strategies"],
                "bindings": config["strategy"]["bindings"],
            },
        ),
    )
    strategy = strategy_registry.get_for(sport="Tennis", competition=None, pair_id=pair)
    defaults = {
        "share": float(config["arbitrage"]["share"]),
        "max_leg_share": float(config["arbitrage"]["max_leg_share"]),
        "fx": float(config["arbitrage"]["fx"]),
    }
    entries = []
    for timestamp in sorted(pair_evaluations):
        if timestamp in skipped:
            continue
        event, books_text = pair_evaluations[timestamp]
        cache.set_books(ast.literal_eval(books_text))
        score, period, status = _score_at(pair_scores, timestamp)
        sports.set(game_id, _SportsState(score, period, status))
        probabilities = best_probabilities_by_outcome(cache, registry, pair)
        if (
            probabilities is not None
            and set(probabilities) == {"yes", "no"}
            and 1.0 < sum(probabilities.values()) < 1.05
        ):
            price_store.update_trend(pair, probabilities)
        context = EvalContext(
            pair_id=pair,
            cache=cache,
            pair_registry=registry,
            sports_store=sports,
            phase_store=sports,
            consecutive_trigger_store=trigger_store,
            scratch={},
            portfolio=portfolio,
            strategy_defaults=defaults,
            event_name=event,
            ts_now_ns=timestamp,
        )
        result = evaluate_tree(strategy.arbitrage_tree, context)
        if not result.hit:
            continue
        for action in result.pending_actions:
            await action.execute(context)
        selected = context.scratch.get("selected_candidate") or {}
        legs = selected.get("legs") or context.scratch.get("legs") or []
        if not legs:
            continue
        accepted = _probability_risk_allows(legs, config.get("risk", {}))
        entry = {
            "timestamp_ns": timestamp,
            "score": score,
            "period": period,
            "risk": "accepted" if accepted else "probability_denied",
            "legs": legs,
        }
        entries.append(entry)
        if not accepted:
            continue
        for leg in legs:
            portfolio.add_fill(
                leg.get("venue"),
                leg.get("role") or leg.get("claim"),
                leg.get("share_if_wins") or leg.get("qty") or 0.0,
            )
    return entries


async def _main(args) -> int:
    config = json.loads(args.config.read_text())
    selected_pairs = {
        f"Tennis|{value}" if not value.startswith("Tennis|") else value for value in args.pair
    }
    evaluations, scores, game_ids, skipped = _parse_log(args.log, selected_pairs)
    rows = []
    for pair in sorted(evaluations):
        entries = await _replay_pair(
            pair,
            game_ids.get(pair, 0),
            evaluations[pair],
            scores.get(pair, []),
            skipped.get(pair, set()),
            config,
            args.competition,
        )
        final_score = scores.get(pair, [(None, "", "", "")])[-1][1]
        winner = _winner_from_score(final_score)
        rows.append(
            {"pair": pair, "entries": entries, "final_score": final_score, "winner": winner}
        )
        print(f"{pair}: final={final_score or '-'} winner={winner or '-'}")
        if not entries:
            print("  无策略计划")
        for entry in entries:
            legs = ", ".join(
                f"{leg.get('venue')} {leg.get('role') or leg.get('claim')} {leg.get('side')}@{leg.get('price')}"
                for leg in entry["legs"]
            )
            print(f"  {entry['score']} {entry['period']} {entry['risk']}: {legs}")
    if args.output:
        args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        print(f"JSON: {args.output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, help="nohup.out 或 .gz 日志")
    parser.add_argument("--config", type=Path, default=Path("arb_config.json"))
    parser.add_argument(
        "--pair", action="append", default=[], help="选手1|选手2; 可重复,缺省回放全部"
    )
    parser.add_argument(
        "--competition", default="ATP Main Tour", help="日志缺少 competition 时使用的分类占位"
    )
    parser.add_argument("--output", type=Path, help="可选 JSON 输出")
    args = parser.parse_args()
    register_builtin_checks_and_actions()
    logging.disable(logging.INFO)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
