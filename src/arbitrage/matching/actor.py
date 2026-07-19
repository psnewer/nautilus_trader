"""
MarketMatchingActor —— NT `Actor` 子类(#59 slice A:自 clock timer 周期触发,gate = anchor + tradable
venue cache 非空 latch)→ 读 cache.instruments → 反推事件 → 跨 venue 匹配 → 生成 pair candidate
→ 概率校验通过后 publish `MatchedPair` + 注册 `PairRegistry`;sports ended 事件驱动 eviction。

设计见 `docs/arbitrage/architectures/matching/architecture.md §3.3 / §4`。
(发现迁 DataClient 原生 `_update_instruments`,InstrumentRefresher 已退役,见 refactor.md §5.2.3/#59。)

**PairRegistry 唯一写者**(#34):matching 算出 pair_id 后,把两边所有腿 instrument_id 映射
到同一 pair_id;下游 risk/portfolio/session 经 registry pull,不再读 info["competition"]。
"""

from __future__ import annotations

from dataclasses import dataclass

from nautilus_trader.adapters.polymarket.sports import SPORTS_CLIENT
from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate
from nautilus_trader.adapters.polymarket.sports import sports_data_type
from nautilus_trader.common.actor import Actor
from nautilus_trader.common.actor import ActorConfig
from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.model.data import DataType
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Venue
from src.arbitrage.common.control import TOPIC_REFRESH_INTERVAL
from src.arbitrage.common.control import SetRefreshIntervalCommand
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.common.venues import probability_from_price
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
    competition_max_matches: dict = None
    probability_validation_enabled: bool = True
    probability_validation_clean_sum: float = 1.05
    probability_validation_min_best_sum: float = 0.95


@dataclass(slots=True)
class _RuntimeDeps:
    """非 msgspec config 可放对象的字段(PairRegistry 由 launcher 经 ArbContext 注入)。"""

    pair_registry: PairRegistry


@dataclass(slots=True)
class _PairCandidate:
    pair_id: str
    sport: str
    competition: str
    confidence: float
    anchor_instrument_ids: list[str]
    tradable_instrument_ids: list[str]
    venue_instrument_ids: dict[str, list[str]]
    registry_instrument_ids: list[str]
    registry_anchor_ids: list[str]
    game_id: int | None = None
    event_key: str = ""                      # #228:所属 event(FAIL 连坐 / 展示分组)
    outcomes: tuple[str, ...] = ("yes", "no")
    validation_group_pair_ids: tuple[str, ...] = ()  # 3-way 同批 role 必须全部 PASS 后发布


@dataclass(slots=True)
class _PairValidationState:
    candidate: _PairCandidate
    log_message: str | None = None
    status: str = "PENDING"
    subscribed_instrument_ids: set[str] = None
    venue_sums: dict[str, float] = None
    best_sum: float | None = None
    fail_reason: str | None = None

    def __post_init__(self) -> None:
        if self.subscribed_instrument_ids is None:
            self.subscribed_instrument_ids = set()
        if self.venue_sums is None:
            self.venue_sums = {}


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
            competition_max_matches=self._competition_max_matches,
        )
        self._emitted_pairs: set[str] = set()  # 已记 INFO 的 pair_id(每 tick 重 emit,日志只记新对)
        self._game_to_pair: dict[int, set[str]] = {}  # #60:gameId → pair_id set(emit 时填,sports ended 时查)
        self._ended_games: set[int] = set()      # #60:已结束 gameId(matching 排除,不再 re-emit)
        self._sports_subscribed: set[int] = set()  # #250:已发起 per-game sports 订阅的 gameId
        self._scan_candidate_gids: set[int] = set()  # #252:本 tick candidate 场次(差集清理基准)
        self._pair_validations: dict[str, _PairValidationState] = {}
        self._validation_pairs_by_instrument: dict[str, set[str]] = {}
        self._event_pairs: dict[str, set[str]] = {}   # #228:event_key → pair_ids(FAIL 连坐)

    # ── 生命周期(#58 slice A:自 timer 触发,替代 InstrumentsRefreshed 订阅)──────
    def on_start(self) -> None:
        # 发现已由 DataClient 拥有(原生 _update_instruments → cache);matching 不再被事件触发,
        # 改 NT clock 自重排周期 timer 读 cache 配对(Q4/Q5 → "两 venue cache 非空" latch)。
        self._schedule_next()
        # #250:sports 订阅按 game 粒度,在发现扫描(`_maybe_match`)见到 anchor 时逐场发起,
        # eviction 时退订;on_start 无全局 sports 订阅。
        # #119:控制台热改周期(方案乙;web §8.3)。
        self._msgbus.subscribe(topic=TOPIC_REFRESH_INTERVAL, handler=self._on_set_refresh_interval_cmd)

    def _on_set_refresh_interval_cmd(self, cmd) -> None:
        if not isinstance(cmd, SetRefreshIntervalCommand) or cmd.secs <= 0:
            return
        self._refresh_interval_secs = cmd.secs  # 下次 _schedule_next 用新值
        if self._log is not None:  # 守卫:离线 __new__ 单测未注册 logger(同 #110 PM 守卫)
            self.log.info(f"matching refresh_interval hot-updated: {cmd.secs}s")

    def _ensure_sports_subscription(self, game_id: int) -> None:
        """#252:candidate 产生时按场订阅 sports 状态(幂等;gid ∈ `_ended_games` 的跳过,
        anchor 实体 evict 后仍留 cache,不跳会重订)。"""
        if game_id in self._sports_subscribed or game_id in self._ended_games:
            return
        self._sports_subscribed.add(game_id)
        try:
            self.subscribe_data(sports_data_type(game_id), client_id=ClientId(SPORTS_CLIENT))
        except Exception as e:  # noqa: BLE001 — 单场订阅失败不挡扫描;下轮扫描重试
            self._sports_subscribed.discard(game_id)
            self._log.warning(f"sports subscribe game {game_id} failed: {e!r}")

    def _release_sports_subscription(self, game_id: int) -> None:
        if game_id not in self._sports_subscribed:
            return
        self._sports_subscribed.discard(game_id)
        try:
            self.unsubscribe_data(sports_data_type(game_id), client_id=ClientId(SPORTS_CLIENT))
        except Exception as e:  # noqa: BLE001
            self._log.warning(f"sports unsubscribe game {game_id} failed: {e!r}")

    def _evict_game(self, game_id: int) -> None:
        # #250:eviction 退订本场 sports(strategy 侧同场退订后归零,client 回收 Store 条目)。
        self._ended_games.add(game_id)
        pair_ids = self._game_to_pair.pop(game_id, set())
        for pair_id in pair_ids:
            self._evict_pair(pair_id, reason=f"game {game_id} ended")
        self._release_sports_subscription(game_id)

    def _reconcile_sports_subscriptions(self, candidate_gids: set[int]) -> None:
        """#252:candidate 差集清理 —— 已订阅但本 tick 无 candidate 的场次:
        有已注册(PASSED)pair → 不动(其 eviction 仍纯 PMS ended 驱动,D4 不变);
        PENDING → 清校验态(退校验 books)+ 释放 sports 订阅 → 归零回收;
        FAILED → 保留 sticky 标记(连坐语义),仅释放 sports 订阅。
        兜住 gamma closed=false 延迟造出的死订阅(PMS 重推窗口有限,永收不到帧)。
        venue 瞬时无 instrument 引发的 PENDING 误清自愈:下 tick candidate 重现即重订重校验。"""
        for gid in sorted(self._sports_subscribed - candidate_gids):
            pair_ids = self._game_to_pair.get(gid, set())
            if any(self._pair_registry.instrument_ids_for_pair(pid) for pid in pair_ids):
                continue
            for pid in sorted(pair_ids):
                state = self._pair_validations.get(pid)
                if state is not None and state.status == "PENDING":
                    self._clear_pair_validation(pid)
                    self._emitted_pairs.discard(pid)
                    self.log.info(f"Evicted pending pair {pid} (game {gid} left candidate set)")
            self._release_sports_subscription(gid)

    def _evict_pair(self, pair_id: str, *, reason: str) -> None:
        self._pair_registry.unregister_pair(pair_id)
        self._emitted_pairs.discard(pair_id)
        self._clear_pair_validation(pair_id)
        self.log.info(f"Evicted pair {pair_id} ({reason})")

    def _clear_pair_validation(self, pair_id: str) -> None:
        state = self._pair_validations.pop(pair_id, None)
        if state is None:
            return
        self._unsubscribe_validation_books(pair_id, state)

    def _unsubscribe_validation_books(self, pair_id: str, state: _PairValidationState) -> None:
        for iid in list(state.subscribed_instrument_ids):
            pair_ids = self._validation_pairs_by_instrument.get(iid)
            if pair_ids is not None:
                pair_ids.discard(pair_id)
                if not pair_ids:
                    self._validation_pairs_by_instrument.pop(iid, None)
            try:
                self.unsubscribe_order_book_deltas(InstrumentId.from_str(iid))
            except Exception as e:  # noqa: BLE001
                self._log.warning(f"matching validation unsubscribe {iid} failed: {e!r}")
            state.subscribed_instrument_ids.discard(iid)

    def on_stop(self) -> None:
        self._cancel_alert()
        for pair_id in list(self._pair_validations):
            self._clear_pair_validation(pair_id)

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
        # #252:sports 订阅随 candidate 产生(emit 点逐场订),不再对 anchor 宇宙全量订阅
        # (gamma closed=false 延迟会造出永收不到帧的死订阅);tick 尾对订阅集做 candidate 差集清理。
        self._scan_candidate_gids = set()
        if any(_event_is_non_tradable(event) for event in anchor_events):
            self._maybe_match_non_tradable_anchor(anchor_events)
        else:
            for tradable_venue in self._tradable_venue_strs:
                tradable_instruments = list(self.cache.instruments(venue=Venue(tradable_venue)))
                if not tradable_instruments:
                    continue
                tradable_events = events_from_instruments(tradable_instruments)
                results = self._engine.match_events(anchor_events, tradable_events)
                for result in results:
                    self._emit_pair(result, tradable_venue=tradable_venue)
        self._reconcile_sports_subscriptions(self._scan_candidate_gids)

    def _maybe_match_non_tradable_anchor(self, anchor_events) -> None:
        """PMSPORTS event-anchor path:同一 anchor event 聚合所有 tradable venues 后发一个 pair。

        过滤条件:pair 的 matched venue 数必须 >= 2(至少两个 tradable venue 匹配上)。
        cap 在聚合+过滤后应用。
        """
        by_event: dict[str, dict] = {}
        for tradable_venue in self._tradable_venue_strs:
            tradable_instruments = list(self.cache.instruments(venue=Venue(tradable_venue)))
            if not tradable_instruments:
                continue
            tradable_events = events_from_instruments(tradable_instruments)
            # skip_cap=True:cap 在聚合+过滤后应用
            results = self._engine.match_events(anchor_events, tradable_events, skip_cap=True)
            for result in results:
                anchor_ev = result.anchor_event
                event_key = _event_key_for(
                    anchor_ev.competition,
                    anchor_ev.home_team_normalized,
                    anchor_ev.away_team_normalized,
                )
                entry = by_event.setdefault(
                    event_key,
                    {
                        "anchor_ev": anchor_ev,
                        "venue_legs": {},          # venue -> list[leg](#228:拆分需要 role,收集 leg 对象)
                        "seen_ids": set(),
                        "confidence": result.total_confidence,
                    },
                )
                entry["confidence"] = max(entry["confidence"], result.total_confidence)
                for leg in result.tradable_event.legs:
                    iid = str(leg.id)
                    if iid in entry["seen_ids"]:
                        continue
                    entry["seen_ids"].add(iid)
                    venue = _venue_of(leg)
                    entry["venue_legs"].setdefault(venue, []).append(leg)

        # 聚合后过滤 + 应用 cap(cap 按 event 计,拆分出的 role pair 不额外占坑)
        emitted_by_comp: dict[str, int] = {}
        for event_key, entry in by_event.items():
            # 过滤:至少 2 个 tradable venue 匹配上
            if len(entry["venue_legs"]) < 2:
                continue
            comp = entry["anchor_ev"].competition
            cap = self._competition_max_matches.get(comp)
            if cap is not None and emitted_by_comp.get(comp, 0) >= cap:
                continue
            emitted_by_comp[comp] = emitted_by_comp.get(comp, 0) + 1
            self._emit_anchor_pairs_for_event(
                event_key,
                anchor_ev=entry["anchor_ev"],
                venue_legs=entry["venue_legs"],
                confidence=entry["confidence"],
            )

    @staticmethod
    def _game_id_of(instrument):
        info = getattr(instrument, "info", None)
        gid = info.get("game_id") if isinstance(info, dict) else None
        return int(gid) if gid is not None else None

    def _emit_anchor_pairs_for_event(
        self,
        event_key: str,
        *,
        anchor_ev,
        venue_legs: dict[str, list],
        confidence: float,
    ) -> None:
        """#228:一个 anchor event → 1(2-way)或 3(3-way,按 role 拆)个 pair candidate。

        PMSPORTS 每场唯一的合成锚在每个 role pair 重复登记(锚是 event 级,一对多复用)。
        """
        anchor_ids = [str(leg.id) for leg in anchor_ev.legs]
        gid = self._game_id_of(anchor_ev.legs[0]) if anchor_ev.legs else None
        all_legs = [leg for legs in venue_legs.values() for leg in legs]
        is_three_way, _ = _partition_legs_by_role(all_legs)
        roles = ("home", "draw", "away") if is_three_way else (None,)

        candidates: list[tuple[_PairCandidate, str]] = []
        for role in roles:
            pair_id = (
                f"{event_key}|{role}" if role else event_key
            )   # non-tradable anchor 路径无 venue 后缀(§4.3)
            venue_ids: dict[str, list[str]] = {}
            for venue, legs in venue_legs.items():
                role_ids = [str(leg.id) for leg in _legs_for_role(legs, role)]
                if role_ids:
                    venue_ids[venue] = role_ids
            if len(venue_ids) < 2:
                continue   # 该 role 不足 2 个 venue → 本 pair 无套利面,跳过
            tradable_ids = [iid for ids in venue_ids.values() for iid in ids]
            candidate = _PairCandidate(
                pair_id=pair_id,
                sport=anchor_ev.sport,
                competition=anchor_ev.competition,
                confidence=confidence,
                anchor_instrument_ids=anchor_ids,
                tradable_instrument_ids=tradable_ids,
                venue_instrument_ids=venue_ids,
                registry_instrument_ids=tradable_ids,
                registry_anchor_ids=anchor_ids,
                game_id=gid,
                event_key=event_key,
                outcomes=("yes", "no"),
            )
            candidates.append((
                candidate,
                (
                    f"MatchedPair {pair_id} (conf={confidence:.2f}, "
                    f"anchor={self._anchor_venue_str.lower()}:{len(anchor_ids)} tradable={len(tradable_ids)})"
                ),
            ))

        if is_three_way and len(candidates) != len(roles):
            return
        if gid is not None:
            self._game_to_pair.setdefault(gid, set()).update(candidate.pair_id for candidate, _ in candidates)
            # #252:candidate 产生即订阅本场 sports(幂等),并计入本 tick 差集基准
            self._scan_candidate_gids.add(gid)
            self._ensure_sports_subscription(gid)
        self._handle_pair_candidates(candidates)

    def _emit_pair(self, result: MatchResult, *, tradable_venue: str) -> None:
        anchor_ev = result.anchor_event
        tradable_ev = result.tradable_event
        anchor_ids = [str(leg.id) for leg in anchor_ev.legs]
        anchor_is_non_tradable = _event_is_non_tradable(anchor_ev)
        event_key = _event_key_for(
            anchor_ev.competition,
            anchor_ev.home_team_normalized,
            anchor_ev.away_team_normalized,
        )
        gid = self._game_id_of(anchor_ev.legs[0]) if anchor_ev.legs else None

        # #228(§4.2.2):tradable(+可交易 anchor)腿含 draw → 按 role 拆 3 个 market 级 pair
        all_tradable_legs = list(tradable_ev.legs) + ([] if anchor_is_non_tradable else list(anchor_ev.legs))
        is_three_way, _ = _partition_legs_by_role(all_tradable_legs)
        roles = ("home", "draw", "away") if is_three_way else (None,)

        candidates: list[tuple[_PairCandidate, str]] = []
        for role in roles:
            pair_id = _pair_id_for(
                anchor_ev.competition,
                anchor_ev.home_team_normalized,
                anchor_ev.away_team_normalized,
                tradable_venue=tradable_venue,
                role=role,
            )
            tradable_ids = [str(leg.id) for leg in _legs_for_role(tradable_ev.legs, role)]
            if is_three_way and not tradable_ids:
                continue   # 该 venue 缺此 role 的 market → 本 pair 无此 venue 腿,跳过
            anchor_leg_ids = (
                [str(leg.id) for leg in _legs_for_role(anchor_ev.legs, role)]
                if not anchor_is_non_tradable
                else []
            )
            tradable_anchor_ids = anchor_leg_ids   # 可交易 anchor(PM)按 role 拆后的腿
            anchor_registry_ids = anchor_ids if anchor_is_non_tradable else []
            venue_ids: dict[str, list[str]] = {}
            if tradable_anchor_ids:
                venue_ids[self._anchor_venue_str] = tradable_anchor_ids
            venue_ids[str(tradable_venue).upper()] = tradable_ids
            if len(venue_ids) < 2:
                continue   # 与 PMSPORTS 聚合路径一致：单 venue role 没有套利面
            candidate = _PairCandidate(
                pair_id=pair_id,
                sport=anchor_ev.sport,
                competition=anchor_ev.competition,
                confidence=result.total_confidence,
                anchor_instrument_ids=anchor_registry_ids,
                tradable_instrument_ids=tradable_anchor_ids + tradable_ids,
                venue_instrument_ids=venue_ids,
                registry_instrument_ids=tradable_anchor_ids + tradable_ids,
                registry_anchor_ids=anchor_registry_ids,
                game_id=gid,
                event_key=event_key,
                outcomes=("yes", "no"),
            )
            candidates.append((
                candidate,
                (
                    f"MatchedPair {pair_id} (conf={result.total_confidence:.2f}, "
                    f"anchor={self._anchor_venue_str.lower()}:{len(anchor_ids)} "
                    f"{tradable_venue.lower()}={len(tradable_ids)})"
                ),
            ))

        if is_three_way and len(candidates) != len(roles):
            return
        if gid is not None:
            self._game_to_pair.setdefault(gid, set()).update(candidate.pair_id for candidate, _ in candidates)
            # #252:candidate 产生即订阅本场 sports(幂等),并计入本 tick 差集基准
            self._scan_candidate_gids.add(gid)
            self._ensure_sports_subscription(gid)
        self._handle_pair_candidates(candidates)

    def _handle_pair_candidates(self, candidates: list[tuple[_PairCandidate, str]]) -> None:
        group_pair_ids = tuple(candidate.pair_id for candidate, _ in candidates)
        for candidate, log_message in candidates:
            candidate.validation_group_pair_ids = group_pair_ids
            self._handle_pair_candidate(candidate, log_message=log_message)

    def _handle_pair_candidate(self, candidate: _PairCandidate, *, log_message: str) -> None:
        if candidate.event_key:
            self._event_pairs.setdefault(candidate.event_key, set()).add(candidate.pair_id)
        if not self.config.probability_validation_enabled:
            self._finalize_pair(candidate, log_message=log_message)
            return
        if self._pair_registry.instrument_ids_for_pair(candidate.pair_id):
            return
        if candidate.pair_id in self._pair_validations:
            return
        # #228 FAIL 连坐(后到方向):同 event 已有 FAIL(错配证据)→ 本 pair 直接 sticky FAILED,
        # 不订阅、不校验、不 publish(先到方向的连坐见 `_fail_event_siblings`)。
        if self._event_has_failed_pair(candidate.event_key):
            self._pair_validations[candidate.pair_id] = _PairValidationState(
                candidate=candidate, status="FAILED", fail_reason="event failed",
            )
            return
        state = _PairValidationState(candidate=candidate)
        state.log_message = log_message
        self._pair_validations[candidate.pair_id] = state
        for iid in candidate.tradable_instrument_ids:
            self._validation_pairs_by_instrument.setdefault(iid, set()).add(candidate.pair_id)
            state.subscribed_instrument_ids.add(iid)
            try:
                self.subscribe_order_book_deltas(InstrumentId.from_str(iid))
            except Exception as e:  # noqa: BLE001
                self._log.warning(f"matching validation subscribe {iid} failed: {e!r}")
        self._try_validate_pair(candidate.pair_id)

    def _register_pair(self, candidate: _PairCandidate, *, log_message: str | None = None) -> None:
        """先完成 PairRegistry 写入，保证同批事件发布前 registry 已完整。"""
        self._pair_registry.register(
            candidate.pair_id,
            candidate.registry_instrument_ids,
            anchor_instrument_ids=candidate.registry_anchor_ids,
            game_id=candidate.game_id,   # #250:strategy 侧按 game_id 扇出全部注册 pair
        )
        if candidate.pair_id not in self._emitted_pairs:
            self._emitted_pairs.add(candidate.pair_id)
            if log_message:
                self.log.info(log_message)

    def _publish_pair(self, candidate: _PairCandidate, *, order_books_managed: bool = False) -> None:
        """同步发布 MatchedPair；MessageBus 返回时 Strategy handler 已执行。"""
        now = self.clock.timestamp_ns()
        self.publish_data(
            data_type=DataType(MatchedPair),
            data=MatchedPair(
                ts_event=now,
                ts_init=now,
                pair_id=candidate.pair_id,
                event_key=candidate.event_key or candidate.pair_id,
                sport=candidate.sport,
                competition=candidate.competition,
                outcomes=list(candidate.outcomes),
                anchor_instrument_ids=candidate.anchor_instrument_ids,
                tradable_instrument_ids=candidate.tradable_instrument_ids,
                venue_instrument_ids=candidate.venue_instrument_ids,
                order_books_managed=order_books_managed,
                confidence=candidate.confidence,
            ),
        )

    def _finalize_pair(self, candidate: _PairCandidate, *, log_message: str | None = None) -> None:
        self._register_pair(candidate, log_message=log_message)
        self._publish_pair(candidate)

    def on_order_book_deltas(self, deltas) -> None:
        instrument_id = str(getattr(deltas, "instrument_id", "") or "")
        for pair_id in list(self._validation_pairs_by_instrument.get(instrument_id, set())):
            self._try_validate_pair(pair_id)

    def on_data(self, data) -> None:
        if isinstance(data, SportsGameUpdate):
            if data.ended:
                self._evict_game(data.game_id)
            return
        if isinstance(data, OrderBookDeltas):
            self.on_order_book_deltas(data)

    def _try_validate_pair(self, pair_id: str) -> None:
        state = self._pair_validations.get(pair_id)
        if state is None or state.status != "PENDING":
            return
        result = _validate_pair_probability(
            state.candidate,
            cache=self.cache,
            clean_sum_threshold=self.config.probability_validation_clean_sum,
            min_best_sum=self.config.probability_validation_min_best_sum,
        )
        if result is None:
            return
        state.venue_sums = result["venue_sums"]
        state.best_sum = result["best_sum"]
        if result["passed"]:
            state.status = "PASSED"
            self._finalize_validation_group_if_passed(state.candidate)
            return
        state.status = "FAILED"
        state.fail_reason = result["reason"]
        self._unsubscribe_validation_books(pair_id, state)
        self.log.info(
            f"MatchedPair {pair_id} probability validation failed: "
            f"best_sum={state.best_sum:.4f}, venue_sums={state.venue_sums}",
        )
        # #228 FAIL 连坐:错配是 event 级证据,同 event 的其余 pair 一并置 FAIL 并 evict。
        self._fail_event_siblings(state.candidate.event_key, pair_id)

    def _finalize_validation_group_if_passed(self, candidate: _PairCandidate) -> None:
        pair_ids = candidate.validation_group_pair_ids or (candidate.pair_id,)
        states = [self._pair_validations.get(pair_id) for pair_id in pair_ids]
        if any(state is None or state.status != "PASSED" for state in states):
            return
        # 三阶段同步交接：整组先注册，再 publish 让 Strategy 以 managed=False 接管，最后释放
        # Matching 的 managed 订阅。publish_data 同步调用 handler，循环结束时接管已完成。
        for state in states:
            self._register_pair(state.candidate, log_message=state.log_message)
        for state in states:
            self._publish_pair(state.candidate, order_books_managed=True)
        for state in states:
            self._unsubscribe_validation_books(state.candidate.pair_id, state)

    def _event_has_failed_pair(self, event_key: str) -> bool:
        if not event_key:
            return False
        for pair_id in self._event_pairs.get(event_key, set()):
            state = self._pair_validations.get(pair_id)
            if state is not None and state.status == "FAILED":
                return True
        return False

    def _fail_event_siblings(self, event_key: str, failed_pair_id: str) -> None:
        if not event_key:
            return
        for sibling in sorted(self._event_pairs.get(event_key, set()) - {failed_pair_id}):
            state = self._pair_validations.get(sibling)
            if state is not None and state.status == "FAILED":
                continue
            if state is not None:
                state.status = "FAILED"   # sticky:后续 candidate 直接跳过,不再 register/publish
                state.fail_reason = f"sibling {failed_pair_id} failed"
                self._unsubscribe_validation_books(sibling, state)
            if self._pair_registry.instrument_ids_for_pair(sibling):
                self._pair_registry.unregister_pair(sibling)
            self._emitted_pairs.discard(sibling)
            self.log.info(
                f"Evicted pair {sibling} (sibling {failed_pair_id} probability validation failed)",
            )


def _validate_pair_probability(
    candidate: _PairCandidate,
    *,
    cache,
    clean_sum_threshold: float,
    min_best_sum: float,
) -> dict | None:
    venue_sums: dict[str, float] = {}
    best_by_outcome: dict[str, float] = {}
    for venue, instrument_ids in candidate.venue_instrument_ids.items():
        probs: list[float] = []
        for iid in instrument_ids:
            instrument = cache.instrument(InstrumentId.from_str(iid))
            outcome = _outcome_label(instrument)   # #228:claim 优先,fallback role
            claim = _claim_of(instrument)
            probability = _ask_probability(cache.order_book(InstrumentId.from_str(iid)), venue, claim=claim)
            if outcome is None or probability is None:
                return None
            probs.append(probability)
            best_by_outcome[outcome] = min(best_by_outcome.get(outcome, probability), probability)
        if not probs:
            return None
        venue_sums[str(venue).upper()] = sum(probs)

    if any(total > clean_sum_threshold for total in venue_sums.values()):
        return None
    if not best_by_outcome:
        return None
    best_sum = sum(best_by_outcome.values())
    return {
        "passed": best_sum >= min_best_sum,
        "reason": "best_sum_below_threshold",
        "venue_sums": venue_sums,
        "best_sum": best_sum,
    }


def _outcome_label(instrument) -> str | None:
    """#228 统一约定(strategy §3.7 同源):claim 优先(3-way 腿),fallback selection_role(2-way)。"""
    info = getattr(instrument, "info", None)
    if not isinstance(info, dict):
        return None
    label = info.get("claim") or info.get("selection_role")
    return str(label).lower() if label else None


def _claim_of(instrument) -> str:
    info = getattr(instrument, "info", None)
    if not isinstance(info, dict):
        return "yes"
    return str(info.get("quote_claim") or "yes").lower()


def _ask_probability(book, venue: str, *, claim: str = "yes") -> float | None:
    price = _best_ask(book)
    if price is None or price <= 0:
        return None
    try:
        return probability_from_price(venue, price, claim)
    except (KeyError, ZeroDivisionError):
        return None


def _best_ask(book) -> float | None:
    if book is None:
        return None
    fn = getattr(book, "best_ask_price", None)
    if callable(fn):
        price = fn()
        return float(price) if price is not None else None
    if isinstance(book, dict):
        value = book.get("ask") or book.get("best_ask")
        return float(value) if value not in (None, "") else None
    return None


def _pair_id_for(
    competition: str,
    home_normalized: str,
    away_normalized: str,
    *,
    tradable_venue: str | None = None,
    include_venue_suffix: bool = True,
    role: str | None = None,
) -> str:
    """matching 架构 §4.3:稳定、确定性 pair_id 生成。

    #228:3-way 拆分 pair 追加 role 后缀(在 venue 后缀之前);2-way 不加(零迁移)。
    """
    base = f"{competition}|{home_normalized}|{away_normalized}"
    if role:
        base = f"{base}|{role}"
    if not include_venue_suffix:
        return base
    venue = str(tradable_venue or "").upper()
    return f"{base}|{venue}" if venue else base


def _event_key_for(competition: str, home_normalized: str, away_normalized: str) -> str:
    """#228:event_key = 无 role/venue 后缀的基础形态(匹配/连坐/分组单位)。"""
    return f"{competition}|{home_normalized}|{away_normalized}"


def _partition_legs_by_role(legs) -> tuple[bool, dict[str, list]]:
    """#228:按 `selection_role` 分组;返回 (is_three_way, role→legs)。"""
    by_role: dict[str, list] = {}
    for leg in legs:
        info = getattr(leg, "info", None)
        role = str((info or {}).get("selection_role") or "").lower() if isinstance(info, dict) else ""
        if role:
            by_role.setdefault(role, []).append(leg)
    return "draw" in by_role, by_role


def _legs_for_role(legs, role: str | None) -> list:
    """#228:role=None(2-way 不拆)返回全部;否则只返回该 role 的腿。"""
    if role is None:
        return list(legs)
    out = []
    for leg in legs:
        info = getattr(leg, "info", None)
        leg_role = str((info or {}).get("selection_role") or "").lower() if isinstance(info, dict) else ""
        if leg_role == role:
            out.append(leg)
    return out


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
