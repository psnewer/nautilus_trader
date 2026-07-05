"""
MarketMatchingActor —— NT `Actor` 子类(#59 slice A:自 clock timer 周期触发,gate = anchor + tradable
venue cache 非空 latch)→ 读 cache.instruments → 反推事件 → 跨 venue 匹配 → publish `MatchedPair`
+ 注册 `PairRegistry`;过期 instrument 经 `_reap_stale_pairs` eviction。

设计见 `docs/arbitrage/architectures/matching/architecture.md §3.3 / §4`。
(发现迁 DataClient 原生 `_update_instruments`,InstrumentRefresher 已退役,见 refactor.md §5.2.3/#59。)

**PairRegistry 唯一写者**(#34):matching 算出 pair_id 后,把两边所有腿 instrument_id 映射
到同一 pair_id;下游 risk/portfolio/session 经 registry pull,不再读 info["competition"]。
"""

from __future__ import annotations

from dataclasses import dataclass

from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate
from nautilus_trader.common.actor import Actor
from nautilus_trader.common.actor import ActorConfig
from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.model.data import DataType
from nautilus_trader.model.identifiers import Venue

from src.arbitrage.common.control import TOPIC_REFRESH_INTERVAL
from src.arbitrage.common.control import SetRefreshIntervalCommand
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.common.venues import venue_id_from_instrument_id
from src.arbitrage.matching.engine import MatchEngine
from src.arbitrage.matching.engine import MatchResult
from src.arbitrage.matching.events import MatchedPair
from src.arbitrage.matching.normalizer import events_from_instruments


_MATCH_ALERT = "market_matching:tick"


class MarketMatchingConfig(ActorConfig, frozen=True, kw_only=True):
    """`anchor_venue`/`tradable_venues` 用于读 cache.instruments;`refresh_interval_secs` = matching 周期轮询间隔
    (#58 slice A:DataClient 拥有发现后,matching 自 timer 读 cache,不再被 InstrumentsRefreshed 触发)。"""

    anchor_venue: str | None = None
    tradable_venues: tuple[str, ...] = ()
    refresh_interval_secs: float = 30.0
    min_similarity: int = 1
    competition_max_matches: dict = None


@dataclass(slots=True)
class _RuntimeDeps:
    """非 msgspec config 可放对象的字段(PairRegistry 由 launcher 经 ArbContext 注入)。"""

    pair_registry: PairRegistry


class MarketMatchingActor(Actor):
    def __init__(self, config: MarketMatchingConfig, deps: _RuntimeDeps) -> None:
        super().__init__(config=config)
        self._anchor_venue_str = str(config.anchor_venue or "").upper()
        self._tradable_venue_strs = tuple(
            str(venue).upper()
            for venue in config.tradable_venues
        )
        self._refresh_interval_secs = config.refresh_interval_secs
        self._pair_registry = deps.pair_registry
        self._competition_max_matches = config.competition_max_matches or {}
        self._engine = MatchEngine(
            min_similarity=config.min_similarity,
            competition_max_matches=self._competition_max_matches,
        )
        self._emitted_pairs: set[str] = set()  # 已记 INFO 的 pair_id(每 tick 重 emit,日志只记新对)
        self._game_to_pair: dict[int, set[str]] = {}  # #60:gameId → pair_id set(emit 时填,sports ended 时查)
        self._ended_games: set[int] = set()      # #60:已结束 gameId(matching 排除,不再 re-emit)

    # ── 生命周期(#58 slice A:自 timer 触发,替代 InstrumentsRefreshed 订阅)──────
    def on_start(self) -> None:
        # 发现已由 DataClient 拥有(原生 _update_instruments → cache);matching 不再被事件触发,
        # 改 NT clock 自重排周期 timer 读 cache 配对(Q4/Q5 → "两 venue cache 非空" latch)。
        self._schedule_next()
        # #60:订 sports 比分信号(NT publish_data 无 metadata topic 带尾 `*`,#58)→ ended 驱动 eviction。
        self._msgbus.subscribe(topic=f"data.{SportsGameUpdate.__name__}*", handler=self.on_data)
        # #119:控制台热改周期(方案乙;web §8.3)。
        self._msgbus.subscribe(topic=TOPIC_REFRESH_INTERVAL, handler=self._on_set_refresh_interval_cmd)

    def _on_set_refresh_interval_cmd(self, cmd) -> None:
        if not isinstance(cmd, SetRefreshIntervalCommand) or cmd.secs <= 0:
            return
        self._refresh_interval_secs = cmd.secs  # 下次 _schedule_next 用新值
        if self._log is not None:  # 守卫:离线 __new__ 单测未注册 logger(同 #110 PM 守卫)
            self.log.info(f"matching refresh_interval hot-updated: {cmd.secs}s")

    # ── sports 信号入口(#60:ended → eviction;替 #59 expiration reaper)────────
    def on_data(self, data) -> None:
        if not isinstance(data, SportsGameUpdate):
            return
        if data.ended:
            self._evict_game(data.game_id)

    def _evict_game(self, game_id: int) -> None:
        self._ended_games.add(game_id)
        pair_ids = self._game_to_pair.pop(game_id, set())
        for pair_id in pair_ids:
            self._pair_registry.unregister_pair(pair_id)
            self._emitted_pairs.discard(pair_id)
            self.log.info(f"Evicted pair {pair_id} (game {game_id} ended)")

    def on_stop(self) -> None:
        self._cancel_alert()

    def _schedule_next(self) -> None:
        self._cancel_alert()
        self.clock.set_time_alert_ns(
            name=_MATCH_ALERT,
            alert_time_ns=self.clock.timestamp_ns() + secs_to_nanos(self._refresh_interval_secs),
            callback=self._on_alert,
        )

    def _on_alert(self, event) -> None:
        try:
            self._maybe_match()
        finally:
            self._schedule_next()

    def _cancel_alert(self) -> None:
        try:
            self.clock.cancel_timer(_MATCH_ALERT)
        except (KeyError, ValueError):
            pass

    # ── 匹配主流程 ────────────────────────────────────────────────────
    def _maybe_match(self) -> None:
        if not self._anchor_venue_str or not self._tradable_venue_strs:
            return
        # latch:anchor + 单个 tradable venue cache 都非空才配(#58)。
        # #60:eviction 改由 sports `ended` 事件驱动(`_evict_game`),非周期 expiration 扫描(用户判定
        # gamma expiration 不准)。这里只**排除已结束 game 的 anchor 腿** → 结束的场不再 re-match/re-register
        # (PM instrument 仍留 cache,NT 无删除 API)。
        anchor_instruments = [
            i for i in self.cache.instruments(venue=Venue(self._anchor_venue_str))
            if self._game_id_of(i) not in self._ended_games
        ]
        if not anchor_instruments:
            return
        anchor_events = events_from_instruments(anchor_instruments)
        if not anchor_events:
            return
        if any(_event_is_non_tradable(event) for event in anchor_events):
            self._maybe_match_non_tradable_anchor(anchor_events)
            return
        for tradable_venue in self._tradable_venue_strs:
            tradable_instruments = list(self.cache.instruments(venue=Venue(tradable_venue)))
            if not tradable_instruments:
                continue
            tradable_events = events_from_instruments(tradable_instruments)
            results = self._engine.match_events(anchor_events, tradable_events)
            for result in results:
                self._emit_pair(result, tradable_venue=tradable_venue)

    def _maybe_match_non_tradable_anchor(self, anchor_events) -> None:
        """PMSPORTS event-anchor path:同一 anchor event 聚合所有 tradable venues 后发一个 pair。

        过滤条件:pair 的 matched venue 数必须 >= 2(至少两个 tradable venue 匹配上)。
        cap 在聚合+过滤后应用。
        """
        by_pair: dict[str, dict] = {}
        for tradable_venue in self._tradable_venue_strs:
            tradable_instruments = list(self.cache.instruments(venue=Venue(tradable_venue)))
            if not tradable_instruments:
                continue
            tradable_events = events_from_instruments(tradable_instruments)
            # skip_cap=True:cap 在聚合+过滤后应用
            results = self._engine.match_events(anchor_events, tradable_events, skip_cap=True)
            for result in results:
                anchor_ev = result.anchor_event
                pair_id = _pair_id_for(
                    anchor_ev.competition,
                    anchor_ev.home_team_normalized,
                    anchor_ev.away_team_normalized,
                    include_venue_suffix=False,  # non-tradable anchor 下同一 event 共用一个基础 pair_id
                )
                entry = by_pair.setdefault(
                    pair_id,
                    {
                        "anchor_ev": anchor_ev,
                        "tradable_ids": [],
                        "venue_ids": {},
                        "confidence": _confidence(result.total_similarity),
                    },
                )
                entry["confidence"] = max(entry["confidence"], _confidence(result.total_similarity))
                for leg in result.tradable_event.legs:
                    iid = str(leg.id)
                    if iid in entry["tradable_ids"]:
                        continue
                    entry["tradable_ids"].append(iid)
                    venue = _venue_of(leg)
                    entry["venue_ids"].setdefault(venue, []).append(iid)

        # 聚合后过滤 + 应用 cap
        emitted_by_comp: dict[str, int] = {}
        for pair_id, entry in by_pair.items():
            # 过滤:至少 2 个 tradable venue 匹配上
            if len(entry["venue_ids"]) < 2:
                continue
            comp = entry["anchor_ev"].competition
            cap = self._competition_max_matches.get(comp)
            if cap is not None and emitted_by_comp.get(comp, 0) >= cap:
                continue
            emitted_by_comp[comp] = emitted_by_comp.get(comp, 0) + 1
            self._emit_anchor_pair(
                pair_id,
                anchor_ev=entry["anchor_ev"],
                tradable_ids=entry["tradable_ids"],
                venue_ids=entry["venue_ids"],
                confidence=entry["confidence"],
            )

    @staticmethod
    def _game_id_of(instrument):
        info = getattr(instrument, "info", None)
        gid = info.get("game_id") if isinstance(info, dict) else None
        return int(gid) if gid is not None else None

    def _emit_anchor_pair(
        self,
        pair_id: str,
        *,
        anchor_ev,
        tradable_ids: list[str],
        venue_ids: dict[str, list[str]],
        confidence: float,
    ) -> None:
        anchor_ids = [str(leg.id) for leg in anchor_ev.legs]
        gid = self._game_id_of(anchor_ev.legs[0]) if anchor_ev.legs else None
        if gid is not None:
            self._game_to_pair.setdefault(gid, set()).add(pair_id)
        self._pair_registry.register(pair_id, tradable_ids, anchor_instrument_ids=anchor_ids)
        if pair_id not in self._emitted_pairs:
            self._emitted_pairs.add(pair_id)
            self.log.info(
                f"MatchedPair {pair_id} (conf={confidence:.2f}, "
                f"anchor={self._anchor_venue_str.lower()}:{len(anchor_ids)} tradable={len(tradable_ids)})",
            )
        now = self.clock.timestamp_ns()
        self.publish_data(
            data_type=DataType(MatchedPair),
            data=MatchedPair(
                ts_event=now,
                ts_init=now,
                pair_id=pair_id,
                sport=anchor_ev.sport,
                competition=anchor_ev.competition,
                anchor_instrument_ids=anchor_ids,
                tradable_instrument_ids=tradable_ids,
                venue_instrument_ids=venue_ids,
                confidence=confidence,
            ),
        )

    def _emit_pair(self, result: MatchResult, *, tradable_venue: str) -> None:
        anchor_ev = result.anchor_event
        tradable_ev = result.tradable_event
        pair_id = _pair_id_for(
            anchor_ev.competition,
            anchor_ev.home_team_normalized,
            anchor_ev.away_team_normalized,
            tradable_venue=tradable_venue,
        )
        anchor_ids = [str(leg.id) for leg in anchor_ev.legs]
        tradable_ids = [str(leg.id) for leg in tradable_ev.legs]
        tradable_anchor_ids = [] if _event_is_non_tradable(anchor_ev) else anchor_ids
        anchor_registry_ids = anchor_ids if _event_is_non_tradable(anchor_ev) else []
        # #60:记 gameId → pair_id(anchor 腿 info["game_id"];sports `ended` 时经此 evict)
        gid = self._game_id_of(anchor_ev.legs[0]) if anchor_ev.legs else None
        if gid is not None:
            self._game_to_pair.setdefault(gid, set()).add(pair_id)
        # 1. 注册到 PairRegistry(下游 pull;同 tick 同步发布前完成)
        self._pair_registry.register(
            pair_id,
            tradable_anchor_ids + tradable_ids,
            anchor_instrument_ids=anchor_registry_ids,
        )
        if pair_id not in self._emitted_pairs:
            self._emitted_pairs.add(pair_id)
            self.log.info(
                f"MatchedPair {pair_id} (conf={_confidence(result.total_similarity):.2f}, "
                f"anchor={self._anchor_venue_str.lower()}:{len(anchor_ids)} "
                f"{tradable_venue.lower()}={len(tradable_ids)})",
            )
        # 2. publish 事件(strategy 订阅)
        now = self.clock.timestamp_ns()
        venue_ids: dict[str, list[str]] = {}
        if tradable_anchor_ids:
            venue_ids[self._anchor_venue_str] = tradable_anchor_ids
        venue_ids[str(tradable_venue).upper()] = tradable_ids
        self.publish_data(
            data_type=DataType(MatchedPair),
            data=MatchedPair(
                ts_event=now, ts_init=now,
                pair_id=pair_id,
                sport=anchor_ev.sport,
                competition=anchor_ev.competition,
                anchor_instrument_ids=anchor_registry_ids,
                tradable_instrument_ids=tradable_anchor_ids + tradable_ids,
                venue_instrument_ids=venue_ids,
                confidence=_confidence(result.total_similarity),
            ),
        )


def _pair_id_for(
    competition: str,
    home_normalized: str,
    away_normalized: str,
    *,
    tradable_venue: str | None = None,
    include_venue_suffix: bool = True,
) -> str:
    """matching 架构 §4.3:稳定、确定性 pair_id 生成。"""
    base = f"{competition}|{home_normalized}|{away_normalized}"
    if not include_venue_suffix:
        return base
    venue = str(tradable_venue or "").upper()
    return f"{base}|{venue}" if venue else base


def _confidence(total_similarity: int) -> float:
    """队名相似度 → 置信度(0-1)。沿用旧 `MatchedPair.from_match_result` 的简单评估。"""
    return min(total_similarity / 10.0, 1.0)


def _event_is_non_tradable(event) -> bool:
    for leg in getattr(event, "legs", []) or []:
        info = getattr(leg, "info", None)
        if isinstance(info, dict) and info.get("tradable") is False:
            return True
    return False


def _venue_of(instrument) -> str:
    try:
        return venue_id_from_instrument_id(instrument.id) or str(instrument.id.venue).upper()
    except Exception:  # noqa: BLE001
        return ""
