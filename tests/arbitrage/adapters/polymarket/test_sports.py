"""PM Sports 比分信号(#60)—— `parse_sport_result` 纯映射 + `SportsGameUpdate` roundtrip。

WS 连接(`PolymarketSportsDataClient._run_ws`)经 /live-test 验(公开 firehose)。
样本取自本会话实采(wnba / atp ended)。
"""

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

import nautilus_trader.adapters.polymarket.sports as sports_module
import src.arbitrage.bootstrap as bootstrap
from nautilus_trader.adapters.polymarket import arb_factories as pm_factories
from nautilus_trader.adapters.polymarket.sports import PolymarketSportsDataClient
from nautilus_trader.adapters.polymarket.sports import PolymarketSportsDataClientConfig
from nautilus_trader.adapters.polymarket.sports import PolymarketSportsInstrumentProvider
from nautilus_trader.adapters.polymarket.sports import SportsGameDataFilter
from nautilus_trader.adapters.polymarket.sports import SportsGameDataProcessor
from nautilus_trader.adapters.polymarket.sports import SportsGameStateStore
from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate
from nautilus_trader.adapters.polymarket.sports import game_id_of_data_type
from nautilus_trader.adapters.polymarket.sports import parse_sport_result
from nautilus_trader.adapters.polymarket.sports import sports_data_type
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import TestClock
from nautilus_trader.model.identifiers import TraderId
from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.common.venues import SPORTS_CLIENT


@pytest.fixture(autouse=True)
def _reset_ctx():
    bootstrap.reset_arb_context()
    yield
    bootstrap.reset_arb_context()


def test_parse_live_sample():
    """实采 wnba 进行中样本 → 字段正确,live True/ended False。"""
    d = {"gameId": 13002300, "leagueAbbreviation": "wnba", "homeTeam": "MIN", "awayTeam": "GSV",
         "status": "InProgress", "score": "66-65", "elapsed": "", "period": "End Q3",
         "live": True, "ended": False}
    u = parse_sport_result(d, ts=123)
    assert u.game_id == 13002300 and u.league == "wnba"
    assert u.home_team == "MIN" and u.away_team == "GSV"
    assert u.live is True and u.ended is False
    assert u.ts_event == 123 and u.ts_init == 123
    assert u.finished_ts == ""


def test_parse_ended_sample():
    """ended → ended True + finished_ts 填(仅 ended 时有)。"""
    d = {"gameId": 5630312, "leagueAbbreviation": "atp", "homeTeam": "Norrie", "awayTeam": "Navone",
         "live": False, "ended": True, "finished_timestamp": "2026-05-20T18:00:00Z"}
    u = parse_sport_result(d, ts=9)
    assert u.ended is True and u.finished_ts == "2026-05-20T18:00:00Z"


def test_parse_missing_game_id_returns_none():
    assert parse_sport_result({"leagueAbbreviation": "x"}, ts=1) is None


def test_roundtrip_to_from_dict():
    d = {"gameId": 7, "leagueAbbreviation": "fif", "homeTeam": "Mexico", "awayTeam": "Serbia",
         "status": "InProgress", "score": "1-0", "elapsed": "80:00", "period": "2H",
         "live": True, "ended": False}
    u = parse_sport_result(d, ts=5)
    u2 = SportsGameUpdate.from_dict(u.to_dict())
    assert u2.game_id == 7 and u2.league == "fif" and u2.home_team == "Mexico"
    assert u2.period == "2H" and u2.live is True and u2.ended is False


def test_sports_provider_builds_non_tradable_anchor_instrument():
    provider = PolymarketSportsInstrumentProvider()
    event = {
        "id": "2701920",
        "gameId": 5843495,
        "title": "Wimbledon ATP: Rafael Jodar vs Felix Gill",
        "startDate": "2026-06-29T10:05:00Z",
        "teams": [
            {"name": "Rafael Jodar", "abbreviation": "jodar", "ordering": "home"},
            {"name": "Felix Gill", "abbreviation": "gill", "ordering": "away"},
        ],
    }

    assert provider._process_event(event, "ATP", "Tennis") == 1
    instruments = list(provider.get_all().values())

    assert len(instruments) == 1
    inst = instruments[0]
    assert str(inst.id.venue) == "PMSPORTS"
    assert inst.info["tradable"] is False
    assert inst.info["anchor"] is True
    assert inst.info["game_id"] == 5843495
    assert inst.info["competition"] == "ATP"
    assert inst.info["home_team"] == "Rafael Jodar"
    assert inst.info["away_team"] == "Felix Gill"


def _sports_client(loop, *, proxy_url=None):
    clock = TestClock()
    return PolymarketSportsDataClient(
        loop=loop,
        msgbus=MessageBus(trader_id=TraderId("TESTER-001"), clock=clock),
        cache=Cache(),
        clock=clock,
        instrument_provider=PolymarketSportsInstrumentProvider(),
        config=PolymarketSportsDataClientConfig(proxy_url=proxy_url),
    )


def test_sports_ws_uses_nt_client_with_explicit_proxy(monkeypatch):
    async def run():
        captured = {}

        class FakeConfig:
            def __init__(self, **kwargs):
                captured["config"] = kwargs

        class FakeClient:
            @classmethod
            async def connect(cls, **kwargs):
                captured["connect"] = kwargs
                return cls()

            async def disconnect(self):
                pass

        monkeypatch.setattr(sports_module, "WebSocketConfig", FakeConfig)
        monkeypatch.setattr(sports_module, "WebSocketClient", FakeClient)

        client = _sports_client(asyncio.get_running_loop(), proxy_url="http://proxy:7890")
        await client._run_ws()

        assert captured["config"]["url"] == sports_module.SPORTS_WS_URL
        assert captured["config"]["proxy_url"] == "http://proxy:7890"
        assert captured["config"]["heartbeat"] is None
        assert captured["connect"]["handler"] == client._on_ws_message
        assert client._ws_client is not None

    asyncio.run(run())


def test_sports_ws_initial_failure_retries(monkeypatch):
    async def run():
        calls = {"connect": 0, "sleep": []}

        class FakeConfig:
            def __init__(self, **_kwargs):
                pass

        class FakeClient:
            @classmethod
            async def connect(cls, **_kwargs):
                calls["connect"] += 1
                if calls["connect"] == 1:
                    raise TimeoutError("opening handshake")
                return cls()

        async def fake_sleep(seconds):
            calls["sleep"].append(seconds)

        monkeypatch.setattr(sports_module, "WebSocketConfig", FakeConfig)
        monkeypatch.setattr(sports_module, "WebSocketClient", FakeClient)
        monkeypatch.setattr(sports_module.asyncio, "sleep", fake_sleep)

        client = _sports_client(asyncio.get_running_loop())
        await client._run_ws()

        assert calls == {"connect": 2, "sleep": [5.0]}
        assert client._ws_client is not None

    asyncio.run(run())


def test_sports_ws_text_ping_uses_nt_client_pong():
    async def run():
        sent = []

        class FakeClient:
            async def send_text(self, raw):
                sent.append(raw)

        client = _sports_client(asyncio.get_running_loop())
        client._ws_client = FakeClient()
        client._on_ws_message(b" ping ")
        await asyncio.sleep(0)

        assert sent == [b"pong"]

    asyncio.run(run())


# ── #250:CustomData 状态管线(Store / 兴趣门控 / Processor / per-game DataType)──

class _MemCache:
    """NT Cache 通用对象区最小替身(add/get/delete bytes)。"""

    def __init__(self):
        self.data: dict[str, bytes] = {}

    def add(self, key: str, value: bytes) -> None:
        self.data[key] = value

    def get(self, key: str):
        return self.data.get(key)

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


def _update(game_id=1, *, ts=100, score="0-0", live=True, ended=False, finished_ts=""):
    return SportsGameUpdate(
        ts_event=ts, ts_init=ts, game_id=game_id, league="wnba", home_team="A", away_team="B",
        status="InProgress", score=score, period="Q1", elapsed="", live=live, ended=ended,
        finished_ts=finished_ts,
    )


def _processor(store=None, *, data_filter=None, is_subscribed=None):
    calls: list = []
    store = store or SportsGameStateStore(_MemCache())
    orig_put = store.put
    def recording_put(update):
        orig_put(update)
        calls.append(("put", update.game_id))
    store.put = recording_put
    proc = SportsGameDataProcessor(
        store=store,
        data_filter=data_filter or SportsGameDataFilter(),
        is_subscribed=is_subscribed if is_subscribed is not None else (lambda gid: True),
        publish=lambda update: calls.append(("publish", update.game_id)),
        log=logging.getLogger("test_sports"),
    )
    return proc, store, calls


def test_processor_writes_store_before_publish():
    """pm-adapter-sports.state.1:准入更新严格先 store.put 再 publish;发布时 Store 已是新状态。"""
    proc, store, calls = _processor()
    proc.process(_update(game_id=7, ts=100, score="1-0"))

    assert calls == [("put", 7), ("publish", 7)]
    assert store.get(7).score == "1-0"


def test_processor_interest_gate_drops_unsubscribed_games():
    """pm-adapter-sports.state.2(兴趣门控):未订阅比赛不存不推(定了就推,不定就不推)。"""
    proc, store, calls = _processor(is_subscribed=lambda gid: gid == 8)
    proc.process(_update(game_id=7, ts=100))
    assert store.get(7) is None and calls == []

    proc.process(_update(game_id=8, ts=100))
    assert store.get(8) is not None and ("publish", 8) in calls


def test_processor_filter_reject_keeps_store():
    """pm-adapter-sports.state.3:附加 filter 拒绝(已订阅)→ 不写 Store,不发布。"""
    class _RejectAll(SportsGameDataFilter):
        def accepts(self, update):
            return False

    proc, store, calls = _processor(data_filter=_RejectAll())
    proc.process(_update(game_id=7))

    assert store.get(7) is None and calls == []


def test_processor_store_write_failure_blocks_publish_and_retries():
    """pm-adapter-sports.state.4:Store 写失败 → 不发布;下一帧仍可重试成功。"""
    proc, store, calls = _processor()
    orig_put = store.put
    store.put = lambda update: (_ for _ in ()).throw(RuntimeError("disk"))
    proc.process(_update(game_id=7, ts=100))
    assert [c for c in calls if c[0] == "publish"] == []

    store.put = orig_put
    proc.process(_update(game_id=7, ts=101))
    assert ("publish", 7) in calls


def test_processor_duplicate_frame_refreshes_cache_without_publish():
    """去重:业务字段与旧状态相同的重复帧 → 刷新 Cache 时戳,不发布。"""
    proc, store, calls = _processor()
    proc.process(_update(game_id=7, ts=100, score="1-0"))
    publishes_before = len([c for c in calls if c[0] == "publish"])

    proc.process(_update(game_id=7, ts=200, score="1-0"))   # 同业务字段,仅时戳新
    assert len([c for c in calls if c[0] == "publish"]) == publishes_before
    assert store.get(7).ts_event == 200                      # Cache 时戳已刷新


def test_processor_stale_frame_dropped():
    """比 Store 旧的更新(ts_event 倒退)→ 整体丢弃,Store 不回退。"""
    proc, store, calls = _processor()
    proc.process(_update(game_id=7, ts=100, score="1-0"))
    proc.process(_update(game_id=7, ts=50, score="0-0"))
    assert store.get(7).score == "1-0" and store.get(7).ts_event == 100


def test_processor_rejects_frames_after_ended_terminal_state():
    """终态拒收:ended 帧放行一次后,该场后续任何帧(重复 ended / 复活帧)准入层丢弃,
    不刷新 Store、不发布 —— 覆盖退订命令异步生效前的小窗。"""
    proc, store, calls = _processor()
    proc.process(_update(game_id=7, ts=100, score="2-1", live=False, ended=True,
                         finished_ts="2026-07-17T00:00:00Z"))
    assert ("publish", 7) in calls                             # ended 本身放行(eviction 依赖)
    n_calls = len(calls)

    proc.process(_update(game_id=7, ts=200, score="2-1", live=False, ended=True,
                         finished_ts="2026-07-17T00:00:00Z"))   # 重复 ended 帧
    proc.process(_update(game_id=7, ts=300, score="3-1", live=True))  # 异常"复活"帧同样拒收
    assert len(calls) == n_calls                                # 无新 put/publish
    assert store.get(7).ts_event == 100 and store.get(7).ended is True


def test_store_roundtrip_and_delete():
    """Store codec roundtrip + 归零回收 delete(真删除,依赖 NT `Cache.delete`)。"""
    store = SportsGameStateStore(_MemCache())
    u = _update(game_id=9, ts=5, score="2-1", live=False, ended=True, finished_ts="2026-07-17T00:00:00Z")
    store.put(u)
    got = store.get(9)
    assert got.score == "2-1" and got.ended is True and got.finished_ts == u.finished_ts
    assert got is not u   # 反序列化重建,天然私有副本

    store.delete(9)
    assert store.get(9) is None


def test_per_game_data_types_route_to_distinct_topics():
    """pm-adapter-sports.state.5:game_id 即订阅键 —— 每场独立 DataType/topic;
    metadata 参与身份,engine 逐场转发命令。"""
    dt_888 = sports_data_type(888)
    dt_999 = sports_data_type(999)
    assert dt_888.topic == "SportsGameUpdate.game_id=888"
    assert dt_999.topic == "SportsGameUpdate.game_id=999"
    assert dt_888 != dt_999
    assert game_id_of_data_type(dt_888) == 888
    assert game_id_of_data_type(sports_data_type("777")) == 777

    from nautilus_trader.model.data import DataType
    assert game_id_of_data_type(DataType(SportsGameUpdate)) is None          # 无 game_id
    assert game_id_of_data_type(DataType(SportsGameUpdate, {"game_id": "x"})) is None


def test_sports_factory_uses_data_source_context(monkeypatch):
    """PMSPORTS factory 从 data-source keyed map 读取目标赛事。"""
    client = MagicMock(name="sports_dc")
    monkeypatch.setattr(pm_factories, "PolymarketSportsDataClient", client)
    bootstrap.prepare_arb_context(
        target_competitions_by_data_source={SPORTS_CLIENT: ["atp"]},
        competition_to_sport_by_data_source={SPORTS_CLIENT: {"atp": "Tennis"}},
        competition_aliases_by_venue={POLYMARKET: {"atp": "ATP"}},
    )

    pm_factories.PolymarketSportsLiveDataClientFactory.create(
        loop=MagicMock(),
        name="PMSPORTS",
        # proxy_url 会传入 pyo3 HttpClient(要求 str|None),mock 需给具体值
        config=MagicMock(proxy_url=None),
        msgbus=MagicMock(),
        cache=MagicMock(),
        clock=MagicMock(),
    )

    provider = client.call_args.kwargs["instrument_provider"]
    assert provider._target_competitions == {"atp"}
    assert provider._competition_to_sport == {"atp": "Tennis"}
    assert provider._competition_aliases == {"atp": "ATP"}


def test_engine_zero_count_unsubscribe_reclaims_store():
    """#250 集成(重编译核心):双消费者先后退订 —— 首退不转发 client(engine 归零判断
    修复,修前 f-string topic 永不匹配、首退即断);末退归零 → NT 原生注册表移除并
    回收 Store 条目。"""
    import asyncio

    from nautilus_trader.adapters.polymarket.sports import PolymarketSportsDataClient
    from nautilus_trader.adapters.polymarket.sports import PolymarketSportsDataClientConfig
    from nautilus_trader.adapters.polymarket.sports import PolymarketSportsInstrumentProvider
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.common.component import MessageBus
    from nautilus_trader.common.component import TestClock
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.data.engine import DataEngine
    from nautilus_trader.data.messages import SubscribeData
    from nautilus_trader.data.messages import UnsubscribeData
    from nautilus_trader.model.identifiers import ClientId
    from nautilus_trader.model.identifiers import TraderId

    async def run():
        clock = TestClock()
        msgbus = MessageBus(trader_id=TraderId("T-001"), clock=clock)
        cache = Cache()
        engine = DataEngine(msgbus=msgbus, cache=cache, clock=clock)
        client = PolymarketSportsDataClient(
            loop=asyncio.get_running_loop(),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=PolymarketSportsInstrumentProvider(),
            config=PolymarketSportsDataClientConfig(),
        )
        engine.register_client(client)

        dt = sports_data_type(888)
        topic = f"data.{dt.topic}"
        handler_a = lambda x: None   # noqa: E731 — 模拟 matching 的 msgbus handler
        handler_b = lambda x: None   # noqa: E731 — 模拟 strategy 的 msgbus handler
        msgbus.subscribe(topic=topic, handler=handler_a)
        msgbus.subscribe(topic=topic, handler=handler_b)

        def _cmd(cls):
            return cls(
                data_type=dt, instrument_id=None, client_id=ClientId(SPORTS_CLIENT),
                venue=None, command_id=UUID4(), ts_init=0, params={"start_ns": None},
            )

        engine.execute(_cmd(SubscribeData))
        await asyncio.sleep(0)
        assert client._is_game_subscribed(888)          # NT 原生注册表已记账

        client._sports_store.put(_update(game_id=888, ts=1))
        assert client._sports_store.get(888) is not None

        # 首个退订:先摘 handler 再发命令(对齐 Actor.unsubscribe_data 时序)
        msgbus.unsubscribe(topic=topic, handler=handler_a)
        engine.execute(_cmd(UnsubscribeData))
        await asyncio.sleep(0)
        assert client._is_game_subscribed(888)           # 未归零 → 不转发(修复点)
        assert client._sports_store.get(888) is not None

        # 末个退订:归零 → 转发 → 原生注册表移除 + client 回收 Store 条目
        msgbus.unsubscribe(topic=topic, handler=handler_b)
        engine.execute(_cmd(UnsubscribeData))
        await asyncio.sleep(0)
        assert not client._is_game_subscribed(888)
        assert client._sports_store.get(888) is None

    asyncio.run(run())


def test_cache_remove_order_book_reclaims_entry():
    """#250:`Cache.remove_order_book`(OBD 订阅归零时 engine 调用)—— 移除后读取返 None,
    缺失时 no-op 不抛。"""
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.model.book import OrderBook
    from nautilus_trader.model.enums import BookType
    from nautilus_trader.model.identifiers import InstrumentId

    cache = Cache()
    iid = InstrumentId.from_str("TEST-BOOK.SIM")
    cache.add_order_book(OrderBook(instrument_id=iid, book_type=BookType.L2_MBP))
    assert cache.order_book(iid) is not None

    cache.remove_order_book(iid)
    assert cache.order_book(iid) is None
    cache.remove_order_book(iid)   # no-op
