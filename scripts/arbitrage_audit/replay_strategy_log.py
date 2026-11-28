"""
用当前配置和真实策略树逐帧回放 nohup 日志。

该工具只做离线审计,不连接远端、不下真单。它把日志中的 MatchedPair、OBD、比分、
phase 与 first_serve 按原始时间顺序送入同一场次回放;比分/phase 复用生产
``SportsGameDataProcessor`` 的变化过滤、Store 和 PhaseStore,再使用生产
``evaluate_tree_to_plan`` 生成执行计划,并把 PM 订单交给
现有 ``PolymarketPaperExchange`` / ``PaperStore``。每场日志盘口逐帧喂给 paper matcher,
由同一套账本语义处理挂单、成交、撤单、余额与仓位。日志没有 L2 队列和完整 Risk 异步
上下文,输出只称“paper 模拟成交”,不是实盘成交。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import gzip
import hashlib
import json
import logging
import re
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from launchers.arb_node import register_builtin_checks_and_actions
from nautilus_trader.adapters.polymarket.sports import SPORTS_CHANNEL_PHASE
from nautilus_trader.adapters.polymarket.sports import SPORTS_CHANNEL_SCORE
from nautilus_trader.adapters.polymarket.sports import SportsGameDataFilter
from nautilus_trader.adapters.polymarket.sports import SportsGameDataProcessor
from nautilus_trader.adapters.polymarket.sports import SportsGameStateStore
from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate
from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.common.sports_phase import SportsPhaseStore
from src.arbitrage.paper.polymarket import PaperServerConfig
from src.arbitrage.paper.polymarket import PaperStore
from src.arbitrage.paper.polymarket import PolymarketPaperExchange
from src.arbitrage.risk.engine import ArbitrageLiveRiskEngine
from src.arbitrage.risk.portfolio import OutcomeExposure
from src.arbitrage.strategy.checks.quote_legs import best_probabilities_by_outcome
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.condition import evaluate_tree_to_plan
from src.arbitrage.strategy.consecutive_triggers import ConsecutiveTriggerStore
from src.arbitrage.strategy.consecutive_triggers import same_set
from src.arbitrage.strategy.consecutive_triggers import score_key
from src.arbitrage.strategy.in_game_positions import InGamePositionRuntimeStore
from src.arbitrage.strategy.json_loader import build_strategy_registry


ANSI = re.compile(r"\x1b\[[0-9;]*m")
ISO_TS = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)Z")
LOCAL_TS = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3})")
SCORE_RE = re.compile(
    r"Sports score updated: game=(\d+).*? score=(.*?) "
    r"period=([^ ]+) elapsed=.*? status=(.*)$",
)
PHASE_RE = re.compile(
    r"(?:Sports|Flashscore) phase updated: game=(\d+) phase=([^ ]+) source=([^ ]+)",
)
SERVE_RE = re.compile(
    r"Flashscore serve snapshot: game=(\d+).*?flashscore_score='([^']*)'.*?"
    r"stored_first_serve='(home|away)'.*?verified=True",
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
            "competition_raw": competition,
        }
        self.competition_name = competition
        if venue in {"ORBITEXCH", "SHARPEXCH"} and outcome == "no":
            self.info["exec_instrument_id"] = INSTRUMENT_IDS[(venue, "yes")]
        self.min_quantity = None
        self.max_quantity = None
        self.size_precision = 2
        self.price_precision = 3 if venue == "POLYMARKET" else 8


class _Book:
    def __init__(self, bid, ask):
        self._bid = bid
        self._ask = ask

    def best_bid_price(self):
        return self._bid

    def best_ask_price(self):
        return self._ask


@dataclass
class _Order:
    client_order_id: str
    instrument_id: str
    side: str
    price: float
    quantity: float
    venue: str
    role: str
    tags: list[str]
    ts_submitted: int
    status: str = "OPEN"
    filled_qty: float = 0.0
    position_id: str | None = None
    submit_score: str = ""
    submit_period: str = ""
    submit_phase: str = ""
    submit_event: str = ""
    trigger_ratio: str = ""
    fill_ts_ns: int | None = None
    fill_price: float | None = None
    fill_score: str = ""
    fill_period: str = ""
    cancel_ts_ns: int | None = None
    cancel_score: str = ""
    cancel_period: str = ""
    cancel_reason: str = ""
    source_tree: str = ""

    def is_closed(self) -> bool:
        return self.status in {"FILLED", "CANCELED", "DENIED", "REJECTED", "EXPIRED"}


@dataclass
class _Position:
    id: str
    instrument_id: str
    side: str
    quantity: float
    opening_order_id: str
    avg_px_open: float


class _Cache:
    def __init__(self, competition: str):
        self._competition = competition
        self._general = {}
        self._books = {}
        self._instruments = {}
        self._orders: list[_Order] = []
        self._positions: dict[str, _Position] = {}
        self._position_ids_by_order: dict[str, str] = {}

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
        values = [position for position in self._positions.values() if position.quantity > 1e-9]
        if instrument_id is None:
            return values
        return [
            position
            for position in values
            if str(position.instrument_id) == str(instrument_id)
        ]

    def positions(self, instrument_id=None):
        return self.positions_open(instrument_id=instrument_id)

    def orders_open(self, instrument_id=None):
        values = [order for order in self._orders if order.status == "OPEN"]
        if instrument_id is None:
            return values
        return [order for order in values if str(order.instrument_id) == str(instrument_id)]

    def orders(self, instrument_id=None):
        if instrument_id is None:
            return list(self._orders)
        return [
            order
            for order in self._orders
            if str(order.instrument_id) == str(instrument_id)
        ]

    def order(self, client_order_id):
        target = str(client_order_id)
        return next(
            (order for order in self._orders if order.client_order_id == target),
            None,
        )

    def position_id(self, client_order_id):
        return self._position_ids_by_order.get(str(client_order_id))

    def add_order(self, order: _Order) -> None:
        self._orders.append(order)
        if order.position_id is not None:
            self._position_ids_by_order[order.client_order_id] = order.position_id

    def position_for_instrument(self, instrument_id: str) -> _Position | None:
        position = self._positions.get(str(instrument_id))
        return position if position is not None and position.quantity > 1e-9 else None

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


class _Portfolio:
    def __init__(self):
        self._shares = {}

    def sync(self, cache: _Cache) -> None:
        self._shares = {}
        for position in cache.positions_open():
            instrument = cache.instrument(position.instrument_id)
            info = getattr(instrument, "info", None) or {}
            role = str(info.get("claim") or info.get("selection_role") or "").lower()
            venue = str(position.instrument_id).rsplit(".", 1)[-1].lower()
            if role:
                self._shares[(venue, role)] = float(position.quantity)

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


def _load_pair_catalog(path: Path) -> dict[str, dict]:
    rows = json.loads(path.read_text())
    if not isinstance(rows, list):
        raise ValueError("pair catalog must be a JSON array")
    result = {}
    for row in rows:
        pair_id = str(row.get("pair_id", "")).strip()
        game_id = row.get("game_id")
        competition = str(row.get("competition", "")).strip()
        if not pair_id or game_id is None or not competition:
            raise ValueError(f"invalid pair catalog row: {row!r}")
        result[pair_id] = {**row, "game_id": int(game_id)}
    return result


def _parse_log(path: Path, selected_pairs: set[str], catalog: dict[str, dict]):  # noqa: C901
    evaluations = defaultdict(dict)
    scores_by_game = defaultdict(list)
    phases_by_game = defaultdict(list)
    serves_by_game = defaultdict(list)
    game_ids = {pair: row["game_id"] for pair, row in catalog.items()}
    pairs_by_game = {row["game_id"]: pair for pair, row in catalog.items()}
    skip_times = defaultdict(list)
    all_pairs = set()
    with _open_text(path) as stream:
        for raw in stream:
            line = ANSI.sub("", raw.rstrip())
            timestamp = _timestamp_ns(line)
            if timestamp is None:
                continue
            score_match = SCORE_RE.search(line)
            if score_match:
                game_id = int(score_match.group(1))
                score = score_match.group(2).split("->")[-1].strip()
                scores_by_game[game_id].append(
                    (
                        timestamp,
                        "" if score == "<none>" else score,
                        score_match.group(3),
                        score_match.group(4).strip(),
                    ),
                )
                continue
            phase_match = PHASE_RE.search(line)
            if phase_match:
                phases_by_game[int(phase_match.group(1))].append(
                    (timestamp, phase_match.group(2), phase_match.group(3)),
                )
                continue
            serve_match = SERVE_RE.search(line)
            if serve_match:
                serves_by_game[int(serve_match.group(1))].append(
                    (timestamp, serve_match.group(2), serve_match.group(3)),
                )
                continue
            schedule_match = SCHEDULE_RE.search(line)
            if schedule_match:
                pair = schedule_match.group(1)
                all_pairs.add(pair)
                if not selected_pairs or pair in selected_pairs:
                    evaluations[pair][timestamp] = (
                        schedule_match.group(2),
                        schedule_match.group(3),
                    )
                continue
            skip_match = SKIP_RE.search(line)
            if skip_match and (not selected_pairs or skip_match.group(1) in selected_pairs):
                skip_times[skip_match.group(1)].append(timestamp)
    for game_id in scores_by_game:
        scores_by_game[game_id] = sorted(
            {row[0]: row for row in scores_by_game[game_id]}.values(),
        )
    for game_id in phases_by_game:
        phases_by_game[game_id] = sorted(
            {row[0]: row for row in phases_by_game[game_id]}.values(),
        )
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
    scores = {
        pair: scores_by_game.get(game_id, [])
        for game_id, pair in pairs_by_game.items()
        if not selected_pairs or pair in selected_pairs
    }
    phases = {
        pair: phases_by_game.get(game_id, [])
        for game_id, pair in pairs_by_game.items()
        if not selected_pairs or pair in selected_pairs
    }
    serves = {
        pair: sorted(serves_by_game.get(game_id, []))
        for game_id, pair in pairs_by_game.items()
        if not selected_pairs or pair in selected_pairs
    }
    return evaluations, scores, phases, serves, game_ids, skipped, all_pairs


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


def _book_snapshot(books) -> dict[str, dict[str, float | None]]:
    result = {}
    for item in books:
        venue = str(item.get("venue", "")).upper()
        outcome = str(item.get("outcome", "")).lower()
        result[f"{venue}:{outcome}"] = {
            "best_bid": item.get("best_bid"),
            "best_ask": item.get("best_ask"),
        }
    return result


def _pm_ask_prices(books) -> dict[str, float] | None:
    snapshot = _book_snapshot(books)
    result = {}
    for outcome in ("yes", "no"):
        value = snapshot.get(f"POLYMARKET:{outcome}", {}).get("best_ask")
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        result[outcome] = value
    return result


def _pm_changed(previous, current) -> bool:
    if previous is None:
        return False
    before = _book_snapshot(previous)
    after = _book_snapshot(current)
    return any(
        before.get(f"POLYMARKET:{outcome}") != after.get(f"POLYMARKET:{outcome}")
        for outcome in ("yes", "no")
    )


def _update_price_state(price_store, cache, registry, pair, books, event, phase, pm_changed):
    state = price_store.get(pair)
    pm_prices = _pm_ask_prices(books)
    pm_clean = pm_prices is not None and 0.98 <= sum(pm_prices.values()) <= 1.02
    is_obd = event in {"MarketOrderBookDeltas", "OrderBookDeltas"}
    if (
        is_obd
        and pm_changed
        and phase == "PRE"
        and state is not None
        and not state.first_price
        and pm_clean
    ):
        price_store.capture_first(pair, pm_prices)
    if is_obd and pm_changed and pm_clean:
        price_store.update_extremes(pair, pm_prices)
    if is_obd:
        probabilities = best_probabilities_by_outcome(cache, registry, pair)
        if probabilities is not None and set(probabilities) == {"yes", "no"}:
            total = sum(probabilities.values())
            if 1.0 < total < 1.02:
                price_store.update_trend(pair, probabilities)
    state = price_store.get(pair)
    if (
        event in {"MarketOrderBookDeltas", "OrderBookDeltas", "SportsGameUpdate"}
        and phase == "IN_PLAY"
        and state is not None
        and state.first_price
        and pm_clean
    ):
        price_store.capture_start(pair, pm_prices)


class _ReplayPaperExchange(PolymarketPaperExchange):
    """关闭外部 WS,只接收日志帧的现有 PM paper exchange。"""

    def __init__(self, config, store, condition_id: str) -> None:
        super().__init__(config, store)
        self._condition_id = condition_id
        self.events = []

    async def resolve_market(self, token_id: str) -> str:
        return self._condition_id

    async def ensure_market_subscription(self, token_id: str) -> None:
        return None

    async def broadcast(self, payload) -> None:
        self.events.append(payload)


class _PaperReplay:
    def __init__(self, pair: str, starting_balance: float) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="arb-paper-replay-")
        database = str(Path(self._tmp.name) / "paper.sqlite3")
        self.store = PaperStore(database, starting_balance, reset=True)
        self.exchange = _ReplayPaperExchange(
            PaperServerConfig(database_path=database, starting_balance=starting_balance),
            self.store,
            condition_id=pair,
        )
        self._pair = pair
        self._starting_balance = float(starting_balance)
        self._sequence = 0
        self._venue_order_by_client: dict[str, str] = {}
        self._opening_order_by_token: dict[str, str] = {}

    def outcome_exposures(self, _pair_id: str, _account_id=None) -> dict[str, OutcomeExposure]:
        """按生产 Portfolio 公式从 paper 现金与当前 PM LONG 仓位重建 outcome exposure。"""
        positions = {
            str(row["token_id"]): row
            for row in self.store.positions()
        }
        costs = {}
        quantities = {}
        for outcome in ("yes", "no"):
            row = positions.get(INSTRUMENT_IDS[("POLYMARKET", outcome)])
            quantity = float(row["current_size"]) if row is not None else 0.0
            avg_price = float(row["avg_price"]) if row is not None else 0.0
            quantities[outcome] = quantity
            costs[outcome] = quantity * avg_price
        balance = self.store.balance_units() / 1_000_000
        realized = balance - self._starting_balance + sum(costs.values())
        return {
            outcome: OutcomeExposure(
                net_profit=(
                    realized
                    + quantities[outcome]
                    - costs[outcome]
                    - sum(cost for other, cost in costs.items() if other != outcome)
                ),
                liability=sum(cost for other, cost in costs.items() if other != outcome),
            )
            for outcome in ("yes", "no")
        }

    def profit_gate_allows(self, specs: list[dict], risk_config: dict, share: float) -> tuple[bool, str]:
        """直接调用生产 Risk 的 match_tp/match_sl 判定;recovery 例外也由生产方法决定。"""
        gate = _ReplayProfitGate(
            pair_id=self._pair,
            portfolio=self,
            risk_config=risk_config,
            share=share,
        )
        for spec in specs:
            order = SimpleNamespace(
                account_id=None,
                instrument_id=str(spec.get("instrument_id") or ""),
                tags=[f"arb:intent={spec.get('intent', 'arbitrage')}"],
            )
            if not gate.check(order):
                return False, gate.denial_reason
        return True, ""

    def next_client_order_id(self) -> str:
        self._sequence += 1
        return f"REPLAY-{self._sequence:06d}"

    async def feed(self, cache: _Cache, timestamp: int, score: str, period: str) -> None:
        for outcome in ("yes", "no"):
            instrument_id = INSTRUMENT_IDS[("POLYMARKET", outcome)]
            bid, ask = _best_prices(cache, instrument_id)
            await self.exchange._on_market_prices(instrument_id, bid, ask)
        self.sync_cache(cache, timestamp, score, period)

    async def submit(
        self,
        plan,
        *,
        source_tree: str,
        cache: _Cache,
        risk_config: dict,
        share: float,
        timestamp: int,
        event: str,
        score: str,
        period: str,
        phase: str,
        trigger_ratio: str,
    ) -> list[dict]:
        specs = [prepared.spec for prepared in plan.orders]
        if not _probability_risk_allows(specs, risk_config):
            return [_denial(timestamp, event, score, period, phase, source_tree, "probability_denied", specs)]
        profit_allowed, profit_reason = self.profit_gate_allows(specs, risk_config, share)
        if not profit_allowed:
            return [_denial(
                timestamp,
                event,
                score,
                period,
                phase,
                source_tree,
                profit_reason,
                specs,
            )]
        for prepared in plan.orders:
            spec = prepared.spec
            if str(prepared.venue).upper() != "POLYMARKET":
                return [_denial(timestamp, event, score, period, phase, source_tree, "non_pm_unsupported", specs)]
            client_order_id = self.next_client_order_id()
            response = await self.exchange.place(
                _paper_payload(
                    token_id=str(spec["instrument_id"]),
                    side=str(spec["side"]),
                    price=float(spec["price"]),
                    size=float(spec["qty"]),
                    salt=self._sequence,
                    order_type=str(spec.get("time_in_force") or "GTC"),
                    post_only=bool(spec.get("post_only", False)),
                ),
            )
            if not response.get("success"):
                return [_denial(
                    timestamp,
                    event,
                    score,
                    period,
                    phase,
                    source_tree,
                    str(response.get("errorMsg") or "paper_rejected"),
                    specs,
                )]
            venue_order_id = str(response["orderID"])
            self._venue_order_by_client[client_order_id] = venue_order_id
            self.store.update_order_metadata(
                venue_order_id,
                {
                    "client_order_id": client_order_id,
                    "pair_id": spec.get("pair_id"),
                    "opportunity_id": spec.get("opportunity_id"),
                    "leg_key": spec.get("leg_key"),
                    "intent": spec.get("intent"),
                    "origin": spec.get("origin"),
                    "instrument_id": str(spec["instrument_id"]),
                    "outcome": str(prepared.role),
                    "strategy_id": source_tree,
                    "order_score": score,
                    "ts_submitted_ns": timestamp,
                },
            )
            cache.add_order(
                _Order(
                    client_order_id=client_order_id,
                    instrument_id=str(spec["instrument_id"]),
                    side=str(spec["side"]).upper(),
                    price=float(spec["price"]),
                    quantity=float(spec["qty"]),
                    venue="POLYMARKET",
                    role=str(prepared.role).lower(),
                    tags=_order_tags(spec, score, timestamp),
                    ts_submitted=timestamp,
                    position_id=str(spec["position_id"]) if spec.get("position_id") else None,
                    submit_score=score,
                    submit_period=period,
                    submit_phase=phase,
                    submit_event=event,
                    trigger_ratio=trigger_ratio,
                    source_tree=source_tree,
                ),
            )
        await asyncio.sleep(0.02)
        self.sync_cache(cache, timestamp, score, period)
        return []

    async def cancel(self, plan, cache: _Cache, timestamp: int, score: str, period: str) -> None:
        selected = set(plan.cancel_order_ids)
        for order in list(cache.orders_open()):
            if selected and order.client_order_id not in selected:
                continue
            venue_order_id = self._venue_order_by_client.get(order.client_order_id)
            if venue_order_id is None:
                continue
            if await self.exchange.cancel(venue_order_id):
                order.cancel_ts_ns = timestamp
                order.cancel_score = score
                order.cancel_period = period
                order.cancel_reason = str(plan.reason or "cancel_pair")
        self.sync_cache(cache, timestamp, score, period)

    def sync_cache(self, cache: _Cache, timestamp: int, score: str, period: str) -> None:
        previously_open = {
            str(position.instrument_id)
            for position in cache.positions_open()
        }
        rows = {str(row.get("client_order_id") or ""): row for row in self.store.orders()}
        trades = {}
        for trade in self.store.trades():
            taker_order_id = str(trade.get("taker_order_id") or "")
            if taker_order_id:
                trades[taker_order_id] = trade
            for maker in trade.get("maker_orders") or ():
                maker_order_id = str(maker.get("order_id") or "")
                if maker_order_id:
                    trades[maker_order_id] = trade
        for order in cache.orders():
            row = rows.get(order.client_order_id)
            if row is None:
                continue
            old_status = order.status
            order.status = {"LIVE": "OPEN", "MATCHED": "FILLED"}.get(
                str(row["status"]),
                str(row["status"]),
            )
            order.filled_qty = float(row["size_matched"])
            if old_status != "FILLED" and order.status == "FILLED":
                trade = trades.get(str(row["id"])) or {}
                order.fill_ts_ns = timestamp
                order.fill_price = float(trade.get("price") or order.price)
                order.fill_score = score
                order.fill_period = period
                if order.side == "BUY" and order.instrument_id not in previously_open:
                    self._opening_order_by_token[order.instrument_id] = order.client_order_id
        positions = {}
        for row in self.store.positions():
            instrument_id = str(row["token_id"])
            opening = self._opening_order_by_token.get(instrument_id, "")
            positions[instrument_id] = _Position(
                id=f"POS-{instrument_id}",
                instrument_id=instrument_id,
                side="LONG",
                quantity=float(row["current_size"]),
                opening_order_id=opening,
                avg_px_open=float(row["avg_price"]),
            )
        cache._positions = positions
        self._opening_order_by_token = {
            token: opening
            for token, opening in self._opening_order_by_token.items()
            if token in positions
        }

    async def close(self) -> None:
        await self.exchange.stop()
        self._tmp.cleanup()


class _ReplayProfitGate:
    """使审计账本可直接调用生产 `_check_profit_gates` 的最小适配层。"""

    def __init__(self, *, pair_id: str, portfolio, risk_config: dict, share: float) -> None:
        self._pair_id = pair_id
        self._portfolio = portfolio
        self._params = SimpleNamespace(
            match_tp=float(risk_config.get("match_tp", 0.05)),
            match_sl=float(risk_config.get("match_sl", -0.05)),
        )
        self._arbitrage_params = SimpleNamespace(share=float(share))
        self.denial_reason = ""

    def _pair_id_for_order(self, _order) -> str:
        return self._pair_id

    def _deny_order(self, *, order, reason: str) -> None:
        self.denial_reason = str(reason)

    def check(self, order) -> bool:
        return ArbitrageLiveRiskEngine._check_profit_gates(self, order)


def _order_tags(spec: dict, score: str, timestamp: int) -> list[str]:
    tags = [
        f"arb:opportunity_id={spec.get('opportunity_id', '')}",
        f"arb:pair_id={spec.get('pair_id', '')}",
        f"arb:leg_key={spec.get('leg_key', '')}",
        f"arb:expected_legs={','.join(spec.get('expected_legs') or ())}",
        f"arb:intent={spec.get('intent', 'arbitrage')}",
        f"arb:order_score={score}",
        f"arb:order_score_ts_ns={timestamp}",
    ]
    for name in ("origin", "magic"):
        if spec.get(name) is not None:
            tags.append(f"arb:{name}={spec[name]}")
    return tags


def _tag_value(order: _Order, name: str) -> str:
    prefix = f"arb:{name}="
    return next(
        (tag[len(prefix):] for tag in order.tags if tag.startswith(prefix)),
        "",
    )


def _order_record(order: _Order) -> dict:
    return {
        "client_order_id": order.client_order_id,
        "instrument_id": order.instrument_id,
        "venue": order.venue,
        "outcome": order.role,
        "side": order.side,
        "limit_price": order.price,
        "quantity": order.quantity,
        "origin": _tag_value(order, "origin"),
        "intent": _tag_value(order, "intent"),
        "magic": _tag_value(order, "magic"),
        "source_tree": order.source_tree,
        "status": order.status,
        "submit_ts_ns": order.ts_submitted,
        "submit_score": order.submit_score,
        "submit_period": order.submit_period,
        "submit_phase": order.submit_phase,
        "submit_event": order.submit_event,
        "trigger_ratio": order.trigger_ratio,
        "fill_ts_ns": order.fill_ts_ns,
        "fill_price": order.fill_price,
        "fill_score": order.fill_score,
        "fill_period": order.fill_period,
        "cancel_ts_ns": order.cancel_ts_ns,
        "cancel_score": order.cancel_score,
        "cancel_period": order.cancel_period,
        "cancel_reason": order.cancel_reason,
        "position_id": order.position_id,
    }


def _paper_payload(token_id, side, price, size, salt, order_type, post_only) -> dict:
    side = str(side).upper()
    if side == "BUY":
        maker_amount = round(price * size * 1_000_000)
        taker_amount = round(size * 1_000_000)
    else:
        maker_amount = round(size * 1_000_000)
        taker_amount = round(price * size * 1_000_000)
    return {
        "order": {
            "salt": salt,
            "maker": "0x0000000000000000000000000000000000000001",
            "signer": "0x0000000000000000000000000000000000000001",
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": token_id,
            "makerAmount": str(maker_amount),
            "takerAmount": str(taker_amount),
            "expiration": "0",
            "nonce": "0",
            "feeRateBps": "0",
            "side": side,
            "signatureType": 0,
            "signature": "0x00",
        },
        "owner": "paper-replay",
        "orderType": order_type,
        "postOnly": post_only,
    }


def _denial(timestamp, event, score, period, phase, source_tree, reason, specs) -> dict:
    return {
        "timestamp_ns": timestamp,
        "event": event,
        "score": score,
        "period": period,
        "phase": phase,
        "source_tree": source_tree,
        "reason": reason,
        "orders": specs,
    }


def _best_prices(cache: _Cache, instrument_id: str) -> tuple[float | None, float | None]:
    book = cache.order_book(instrument_id)
    if book is None:
        return None, None
    try:
        bid = book.best_bid_price()
        ask = book.best_ask_price()
        return (
            float(bid) if bid is not None else None,
            float(ask) if ask is not None else None,
        )
    except (TypeError, ValueError):
        return None, None


def _trigger_ratio_at_order(
    *,
    trigger_store: ConsecutiveTriggerStore,
    sports_store: SportsGameStateStore,
    pair_id: str,
    game_id: int,
    history_key: str,
    when: str | None,
) -> str:
    state = sports_store.get(game_id)
    current_score = score_key(getattr(state, "score", "") if state is not None else "")
    if current_score is None:
        return ""
    history = [
        observation
        for observation in trigger_store.history(pair_id, history_key)
        if same_set(observation.score_key, current_score)
    ]
    if when == "current_draw":
        cutoff_ns = None
        for score_state in reversed(tuple(sports_store.score_history(game_id))):
            parsed = score_key(getattr(score_state, "score", ""))
            if parsed is None or not same_set(parsed, current_score):
                continue
            left, right, _, _ = parsed[-1]
            if left != right:
                continue
            cutoff_ns = int(getattr(score_state, "ts_event", 0) or 0)
            break
        if not cutoff_ns:
            return ""
        history = [observation for observation in history if observation.ts_ns < cutoff_ns]
    counts = {
        outcome: len({
            observation.score_key
            for observation in history
            if observation.passed and observation.outcome == outcome
        })
        for outcome in ("yes", "no")
    }
    if not any(counts.values()):
        return ""
    return f"{counts['yes']}:{counts['no']}"


def _trigger_count_settings(actions) -> tuple[str, str | None]:
    for action in actions:
        if action.__class__.__name__ == "TriggerCountAction":
            return str(action._history_key), action._when
    return "pre_rebate_original_leg", None


async def _replay_pair(  # noqa: C901
    pair,
    game_id,
    pair_evaluations,
    pair_scores,
    pair_phases,
    pair_serves,
    skipped,
    config,
    competition,
):
    cache = _Cache(competition)
    registry = _Registry(pair, game_id)
    sports = SportsGameStateStore(cache)
    phase_store = SportsPhaseStore(cache)
    published_sports: list[tuple[SportsGameUpdate, str]] = []
    sports_processor = SportsGameDataProcessor(
        store=sports,
        phase_store=phase_store,
        data_filter=SportsGameDataFilter(),
        subscribed_channels=lambda _game_id: {SPORTS_CHANNEL_PHASE, SPORTS_CHANNEL_SCORE},
        publish=lambda update, channel: published_sports.append((update, channel)),
        log=logging.getLogger("arb-replay-sports"),
    )
    portfolio = _Portfolio()
    trigger_store = ConsecutiveTriggerStore()
    in_game_position_store = InGamePositionRuntimeStore()
    paper = _PaperReplay(
        pair,
        float(config.get("execution", {}).get("paper", {}).get("starting_balance", 10_000.0)),
    )
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
    denials = []
    previous_books = None
    current_books = None

    async def evaluate(timestamp: int, event: str, books, *, market_frame: bool) -> None:
        nonlocal previous_books
        if books is None:
            return
        state = sports.get(game_id)
        phase_state = phase_store.get(game_id)
        score = str(getattr(state, "score", "") or "")
        period = str(getattr(state, "period", "") or "")
        phase = str(getattr(phase_state, "phase", "UNKNOWN") or "UNKNOWN")
        _update_price_state(
            price_store,
            cache,
            registry,
            pair,
            books,
            event,
            phase,
            market_frame and _pm_changed(previous_books, books),
        )
        if market_frame:
            previous_books = books
            await paper.feed(cache, timestamp, score, period)
        portfolio.sync(cache)
        base_context = {
            "pair_id": pair,
            "cache": cache,
            "pair_registry": registry,
            "sports_store": sports,
            "phase_store": phase_store,
            "consecutive_trigger_store": trigger_store,
            "in_game_position_store": in_game_position_store,
            "portfolio": portfolio,
            "strategy_defaults": defaults,
            "event_name": event,
            "ts_now_ns": timestamp,
        }
        arb_context = EvalContext(**base_context, tree_kind="arbitrage")
        comp_context = EvalContext(**base_context, tree_kind="compensation")
        arb_result, comp_result = await asyncio.gather(
            evaluate_tree_to_plan(strategy.arbitrage_tree, arb_context),
            evaluate_tree_to_plan(strategy.compensation_tree, comp_context),
        )
        comp_plan = comp_context.scratch.get("execution_plan") if comp_result.hit else None
        arb_plan = arb_context.scratch.get("execution_plan") if arb_result.hit else None
        plan = comp_plan or arb_plan
        source_tree = "compensation" if comp_plan is not None else "arbitrage"
        if plan is None:
            return
        if plan.kind == "cancel_pair":
            await paper.cancel(plan, cache, timestamp, score, period)
            portfolio.sync(cache)
            return
        trigger_ratio = ""
        if source_tree == "arbitrage":
            history_key, when = _trigger_count_settings(arb_result.pending_actions)
            trigger_ratio = _trigger_ratio_at_order(
                trigger_store=trigger_store,
                sports_store=sports,
                pair_id=pair,
                game_id=game_id,
                history_key=history_key,
                when=when,
            )
        rejected = await paper.submit(
            plan,
            source_tree=source_tree,
            cache=cache,
            risk_config=config.get("risk", {}),
            share=float(config["arbitrage"]["share"]),
            timestamp=timestamp,
            event=event,
            score=score,
            period=period,
            phase=phase,
            trigger_ratio=trigger_ratio,
        )
        portfolio.sync(cache)
        denials.extend(rejected)

    home, away = [*pair.split("|", 2), "", ""][1:3]
    frames = []
    for timestamp, (event, books_text) in pair_evaluations.items():
        frames.append((timestamp, 30, "strategy", (event, books_text)))
    for timestamp, score, period, status in pair_scores:
        frames.append((timestamp, 10, "score", (score, period, status)))
    for timestamp, phase, source in pair_phases:
        frames.append((timestamp, 20, "phase", (phase, source)))
    for timestamp, external_score, first_serve in pair_serves:
        frames.append((timestamp, 25, "serve", (external_score, first_serve)))

    for timestamp, _priority, kind, payload in sorted(frames):
        if kind == "strategy":
            event, books_text = payload
            if timestamp in skipped:
                continue
            current_books = ast.literal_eval(books_text)
            cache.set_books(current_books)
            await evaluate(
                timestamp,
                event,
                current_books,
                market_frame=event in {"MarketOrderBookDeltas", "OrderBookDeltas"},
            )
            continue

        if kind == "serve":
            external_score, first_serve = payload
            sports.set_first_serve(
                game_id,
                external_score=external_score,
                first_serve=first_serve,
            )
            continue

        previous = sports.get(game_id)
        previous_phase = phase_store.get(game_id)
        if kind == "score":
            score, period, status = payload
            phase = previous_phase.phase if previous_phase is not None else ""
            ended = phase == "POST" or str(status).lower() in {"finished", "ended", "final"}
            live = phase == "IN_PLAY" or (not ended and str(status).lower() in {"inprogress", "in_play", "live"})
            source = "PMSPORTS"
        else:
            phase, source = payload
            score = str(getattr(previous, "score", "") or "")
            period = str(getattr(previous, "period", "") or "")
            ended = phase == "POST"
            live = phase == "IN_PLAY"
            status = "Finished" if ended else ("InProgress" if live else "")
        update = SportsGameUpdate(
            ts_event=timestamp,
            ts_init=timestamp,
            game_id=int(game_id),
            league=competition,
            home_team=home,
            away_team=away,
            status=str(status),
            score=str(score),
            period=str(period),
            elapsed="",
            live=live,
            ended=ended,
            finished_ts="",
            first_serve=str(getattr(previous, "first_serve", "") or ""),
        )
        if source == "FLASHSCORE":
            changed = phase_store.advance(game_id, phase, source=source, ts_event=timestamp)
            if changed:
                published_sports.extend(
                    ((update, SPORTS_CHANNEL_PHASE), (update, SPORTS_CHANNEL_SCORE)),
                )
        else:
            sports_processor.process(update)
        while published_sports:
            # Processor 负责准入、Store/PhaseStore 写入和通道变化判定;真正的策略调度
            # 以日志中随后出现的 Strategy evaluate scheduled 为锚,保持生产异步时序。
            published_sports.pop(0)
    orders = cache.orders()
    positions = cache.positions_open()
    balance = paper.store.balance_units() / 1_000_000
    await paper.close()
    return orders, denials, positions, balance, price_store.get(pair)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _format_time(timestamp_ns: int) -> str:
    value = datetime.fromtimestamp(timestamp_ns / 1_000_000_000, UTC)
    return value.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _write_xlsx(path: Path, rows: list[dict], metadata: dict) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.styles import PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    detail = workbook.active
    detail.title = "ATP赛中订单生命周期"
    headers = [
        "比赛",
        "game_id",
        "订单ID",
        "来源树",
        "origin",
        "intent",
        "magic",
        "venue",
        "outcome",
        "side",
        "限价",
        "qty",
        "最终状态",
        "提交时间(北京)",
        "提交事件",
        "提交比分",
        "触发次数比(YES:NO)",
        "提交盘",
        "模拟成交时间(北京)",
        "模拟成交价",
        "模拟成交比分",
        "模拟成交盘",
        "撤单时间(北京)",
        "撤单比分",
        "撤单盘",
        "撤单原因",
        "position_id",
    ]
    detail.append(headers)
    for row in rows:
        for order in row["orders"]:
            detail.append(
                [
                    row["pair"].removeprefix("Tennis|").replace("|", " vs "),
                    row["game_id"],
                    order["client_order_id"],
                    order["source_tree"],
                    order["origin"],
                    order["intent"],
                    order["magic"],
                    order["venue"],
                    order["outcome"],
                    order["side"],
                    order["limit_price"],
                    order["quantity"],
                    order["status"],
                    _format_time(order["submit_ts_ns"]),
                    order["submit_event"],
                    order["submit_score"],
                    order["trigger_ratio"],
                    order["submit_period"],
                    _format_time(order["fill_ts_ns"]) if order["fill_ts_ns"] else "",
                    order["fill_price"],
                    order["fill_score"],
                    order["fill_period"],
                    _format_time(order["cancel_ts_ns"]) if order["cancel_ts_ns"] else "",
                    order["cancel_score"],
                    order["cancel_period"],
                    order["cancel_reason"],
                    order["position_id"],
                ],
            )

    summary = workbook.create_sheet("比赛汇总")
    summary.append(
        [
            "比赛",
            "game_id",
            "最终比分",
            "胜方",
            "赛中订单数",
            "BUY",
            "SELL",
            "模拟成交",
            "未成交挂单",
            "已撤单",
            "赛中风控拒绝计划",
            "已采first_price",
            "已采start_price",
            "已有trend_price",
        ],
    )
    for row in rows:
        summary.append(
            [
                row["pair"].removeprefix("Tennis|").replace("|", " vs "),
                row["game_id"],
                row["final_score"],
                row["winner"],
                len(row["orders"]),
                sum(order["side"] == "BUY" for order in row["orders"]),
                sum(order["side"] == "SELL" for order in row["orders"]),
                sum(order["status"] == "FILLED" for order in row["orders"]),
                sum(order["status"] == "OPEN" for order in row["orders"]),
                sum(order["status"] == "CANCELED" for order in row["orders"]),
                len(row["denials"]),
                bool(row["final_price_state"].get("first_price")),
                bool(row["final_price_state"].get("start_price")),
                bool(row["final_price_state"].get("trend_price")),
            ],
        )

    notes = workbook.create_sheet("回放说明")
    notes.append(["项目", "值"])
    for key, value in metadata.items():
        notes.append([key, value])
    notes.append(["口径", "按日志时间顺序重新执行当前策略树;不是读取历史下单瞬间。"])
    notes.append(["范围", "运行完整 PRE/IN_PLAY 状态,报表只展示 origin=in_game 的 ATP 订单;WTA 不进入回放。"])
    notes.append(["执行机制", "复用 PolymarketPaperExchange / PaperStore;逐场把日志 PM bid/ask 帧注入 paper matcher。"])
    notes.append(
        [
            "模拟限制",
            "日志只有 best bid/ask,无法还原队列、深度和部分成交;触价按全额成交,结果不是实盘成交。",
        ],
    )
    notes.append(["撮合", "新单交叉时按对手最优价成交;后续盘口触价时按订单限价成交,与 PM paper 口径一致。"])
    notes.append(
        [
            "pair_in_flight",
            "默认不复用旧进程的 pair_in_flight 跳帧,因为它属于旧配置执行结果,不应污染当前配置反事实回放。",
        ],
    )

    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E78")
        for index, column in enumerate(sheet.columns, start=1):
            width = min(max(len(str(cell.value or "")) for cell in column) + 2, 60)
            sheet.column_dimensions[get_column_letter(index)].width = width
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


async def _main(args) -> int:
    config = json.loads(args.config.read_text())
    catalog = _load_pair_catalog(args.pair_catalog) if args.pair_catalog else {}
    selected_pairs = {
        f"Tennis|{value}" if not value.startswith("Tennis|") else value for value in args.pair
    }
    if catalog:
        selected_pairs = (selected_pairs & set(catalog)) if selected_pairs else set(catalog)
    evaluations, scores, phases, serves, game_ids, skipped, all_pairs = _parse_log(
        args.log,
        selected_pairs,
        catalog,
    )
    rows = []
    for pair in sorted(evaluations):
        competition = str(catalog.get(pair, {}).get("competition") or args.competition)
        all_orders, denials, positions, balance, final_price_state = await _replay_pair(
            pair,
            game_ids.get(pair, 0),
            evaluations[pair],
            scores.get(pair, []),
            phases.get(pair, []),
            serves.get(pair, []),
            skipped.get(pair, set()) if args.use_historical_pair_in_flight else set(),
            config,
            competition,
        )
        pair_score_rows = scores.get(pair) or [(None, "", "", "")]
        final_score = pair_score_rows[-1][1]
        winner = _winner_from_score(final_score)
        orders = [
            _order_record(order)
            for order in all_orders
            if _tag_value(order, "origin") == "in_game"
        ]
        in_game_denials = [denial for denial in denials if denial["phase"] == "IN_PLAY"]
        rows.append(
            {
                "pair": pair,
                "game_id": game_ids.get(pair, 0),
                "orders": orders,
                "denials": in_game_denials,
                "positions": [
                    {
                        "position_id": position.id,
                        "instrument_id": position.instrument_id,
                        "quantity": position.quantity,
                        "opening_order_id": position.opening_order_id,
                        "avg_px_open": position.avg_px_open,
                    }
                    for position in positions
                ],
                "ending_balance": balance,
                "final_score": final_score,
                "winner": winner,
                "final_price_state": {
                    "first_price": dict(final_price_state.first_price),
                    "start_price": dict(final_price_state.start_price),
                    "trend_price": dict(final_price_state.trend_price),
                },
            }
        )
        print(f"{pair}: final={final_score or '-'} winner={winner or '-'}")
        if not orders and not in_game_denials:
            print("  无赛中订单或风控拒绝")
        for order in orders:
            print(
                f"  {order['submit_score']} {order['submit_period']} "
                f"{order['side']} {order['outcome']}@{order['limit_price']} {order['status']}",
            )
        for denial in in_game_denials:
            print(f"  {denial['score']} {denial['period']} {denial['reason']}")
    if args.output:
        args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        print(f"JSON: {args.output}")
    if args.xlsx:
        metadata = {
            "日志": str(args.log.resolve()),
            "日志 SHA256": _sha256(args.log),
            "配置": str(args.config.resolve()),
            "配置 SHA256": _sha256(args.config),
            "ATP catalog": str(args.pair_catalog.resolve()) if args.pair_catalog else "-",
            "catalog SHA256": _sha256(args.pair_catalog) if args.pair_catalog else "-",
            "日志全部 pair": len(all_pairs),
            "纳入 ATP pair": len(evaluations),
            "排除 pair": len(all_pairs - set(evaluations)),
            "生成时间": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        }
        _write_xlsx(args.xlsx, rows, metadata)
        print(f"Excel: {args.xlsx}")
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
    parser.add_argument("--pair-catalog", type=Path, help="pair_id/game_id/competition JSON 数组")
    parser.add_argument(
        "--use-historical-pair-in-flight",
        action="store_true",
        help="复用旧进程的 pair_in_flight 跳帧;反事实回放默认关闭",
    )
    parser.add_argument("--output", type=Path, help="可选 JSON 输出")
    parser.add_argument("--xlsx", type=Path, help="可选 Excel 输出")
    args = parser.parse_args()
    register_builtin_checks_and_actions()
    logging.disable(logging.INFO)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
