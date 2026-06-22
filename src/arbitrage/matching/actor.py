"""
MarketMatchingActor —— NT `Actor` 子类(#59 slice A:自 clock timer 周期触发,gate = 两 venue
cache 非空 latch)→ 读 cache.instruments → 反推事件 → 跨 venue 匹配 → publish `MatchedPair`
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
from src.arbitrage.matching.engine import MatchEngine
from src.arbitrage.matching.engine import MatchResult
from src.arbitrage.matching.events import MatchedPair
from src.arbitrage.matching.normalizer import events_from_instruments


_MATCH_ALERT = "market_matching:tick"


class MarketMatchingConfig(ActorConfig, frozen=True, kw_only=True):
    """`pm_venue`/`oe_venue` 用于读 cache.instruments;`refresh_interval_secs` = matching 周期轮询间隔
    (#58 slice A:DataClient 拥有发现后,matching 自 timer 读 cache,不再被 InstrumentsRefreshed 触发)。"""

    pm_venue: str = "POLYMARKET"
    oe_venue: str = "ORBITEXCH"
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
        self._pm_venue_str = config.pm_venue
        self._oe_venue_str = config.oe_venue
        self._refresh_interval_secs = config.refresh_interval_secs
        self._pair_registry = deps.pair_registry
        self._engine = MatchEngine(
            min_similarity=config.min_similarity,
            competition_max_matches=config.competition_max_matches or {},
        )
        self._emitted_pairs: set[str] = set()  # 已记 INFO 的 pair_id(每 tick 重 emit,日志只记新对)
        self._game_to_pair: dict[int, str] = {}  # #60:gameId → pair_id(emit 时填,sports ended 时查)
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
        pair_id = self._game_to_pair.pop(game_id, None)
        if pair_id is not None:
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
        # latch:两 venue cache 都非空才配(#58)。
        # #60:eviction 改由 sports `ended` 事件驱动(`_evict_game`),非周期 expiration 扫描(用户判定
        # gamma expiration 不准)。这里只**排除已结束 game 的 PM 腿** → 结束的场不再 re-match/re-register
        # (PM instrument 仍留 cache,NT 无删除 API)。
        pm_instruments = [
            i for i in self.cache.instruments(venue=Venue(self._pm_venue_str))
            if self._game_id_of(i) not in self._ended_games
        ]
        oe_instruments = list(self.cache.instruments(venue=Venue(self._oe_venue_str)))
        if not pm_instruments or not oe_instruments:
            return
        pm_events = events_from_instruments(pm_instruments)
        oe_events = events_from_instruments(oe_instruments)
        results = self._engine.match_events(pm_events, oe_events)
        for result in results:
            self._emit_pair(result)

    @staticmethod
    def _game_id_of(instrument):
        info = getattr(instrument, "info", None)
        gid = info.get("game_id") if isinstance(info, dict) else None
        return int(gid) if gid is not None else None

    def _emit_pair(self, result: MatchResult) -> None:
        pm_ev = result.polymarket_event
        oe_ev = result.orbitexch_event
        pair_id = _pair_id_for(pm_ev.competition, pm_ev.home_team_normalized, pm_ev.away_team_normalized)
        pm_ids = [str(leg.id) for leg in pm_ev.legs]
        oe_ids = [str(leg.id) for leg in oe_ev.legs]
        # #60:记 gameId → pair_id(PM 腿 info["game_id"];sports `ended` 时经此 evict)
        gid = self._game_id_of(pm_ev.legs[0]) if pm_ev.legs else None
        if gid is not None:
            self._game_to_pair[gid] = pair_id
        # 1. 注册到 PairRegistry(下游 pull;同 tick 同步发布前完成)
        self._pair_registry.register(pair_id, pm_ids + oe_ids)
        if pair_id not in self._emitted_pairs:
            self._emitted_pairs.add(pair_id)
            self.log.info(
                f"MatchedPair {pair_id} (conf={_confidence(result.total_similarity):.2f}, "
                f"pm={len(pm_ids)} oe={len(oe_ids)})",
            )
        # 2. publish 事件(strategy 订阅)
        now = self.clock.timestamp_ns()
        self.publish_data(
            data_type=DataType(MatchedPair),
            data=MatchedPair(
                ts_event=now, ts_init=now,
                pair_id=pair_id,
                sport=pm_ev.sport,
                competition=pm_ev.competition,
                pm_instrument_ids=pm_ids,
                oe_instrument_ids=oe_ids,
                confidence=_confidence(result.total_similarity),
            ),
        )


def _pair_id_for(competition: str, home_normalized: str, away_normalized: str) -> str:
    """matching 架构 §4.3:稳定、确定性 pair_id 生成。"""
    return f"{competition}|{home_normalized}|{away_normalized}"


def _confidence(total_similarity: int) -> float:
    """队名相似度 → 置信度(0-1)。沿用旧 `MatchedPair.from_match_result` 的简单评估。"""
    return min(total_similarity / 10.0, 1.0)
