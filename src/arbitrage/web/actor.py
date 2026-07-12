"""
WebGatewayActor —— Step 7 控制台网关(NT `Actor` 子类,与 TradingNode 同进程同 loop)。

`on_start` 内拉起 FastAPI/uvicorn 协程(`build_app` 路由)。**控制面**:TradingState 启停 +
配置编辑(写经 MessageBus 命令,方案乙;读经 risk_engine 引用)+ `/ws` 推 TradingState 变更。
纯只读监控 endpoint(余额/matched_pairs/way_rebate)已按用户要求移除(2026-06-21)。

详细设计:`docs/arbitrage/architectures/web/architecture.md §8`。
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

import uvicorn

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.actor import ActorConfig
from nautilus_trader.model.enums import trading_state_to_str
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.identifiers import InstrumentId

from src.arbitrage.matching.events import MatchedPair

from src.arbitrage.common.control import TOPIC_ARBITRAGE_PARAMS
from src.arbitrage.common.control import TOPIC_REFRESH_INTERVAL
from src.arbitrage.common.control import TOPIC_RISK_PARAMS
from src.arbitrage.common.control import TOPIC_TRADING_STATE
from src.arbitrage.common.control import SetArbitrageParamsCommand
from src.arbitrage.common.control import SetRefreshIntervalCommand
from src.arbitrage.common.control import SetRiskParamsCommand
from src.arbitrage.common.control import SetTradingStateCommand
from src.arbitrage.common.params import ArbitrageParams
from src.arbitrage.common.venues import descriptor_for
from src.arbitrage.common.venues import is_known_venue
from src.arbitrage.web.app import build_app


_WS_QUEUE_MAXSIZE = 256  # 每 client 队列上限;满则丢最旧(绝不反压交易回调)


class WebGatewayConfig(ActorConfig, frozen=True, kw_only=True):
    enabled: bool = False
    host: str = "127.0.0.1"  # 默认只绑本机;暴露公网需显式改
    port: int = 8080


@dataclass(slots=True)
class WebGatewayDeps:
    """非 msgspec 对象经 deps 注入。读经引用(risk_engine),**写经 MessageBus 命令**(方案乙;web §8.3)。

    `config_path` = `arb_config.json` 路径,PUT 配置写回它(对齐 legacy `_save_config`)。
    """

    loop: asyncio.AbstractEventLoop
    risk_engine: object | None = None        # 读 trading_state / live risk params(写走命令)
    arbitrage_params: ArbitrageParams | None = None  # 读/热改 Web Arbitrage 默认值
    config_path: str | None = None           # arb_config.json,PUT 写回
    pair_registry: object | None = None      # /odds:遍历 matched pair 的腿读 cache 盘口


class _NoSignalServer(uvicorn.Server):
    """NT 自管 SIGINT/SIGTERM;uvicorn 嵌入同 loop 时不抢信号处理。"""

    def install_signal_handlers(self) -> None:
        pass


def _port_bindable(host: str, port: int) -> bool:
    """同步预检端口能否绑定(uvicorn serve() 在 task 内 bind 失败会被吞,故先探一次)。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _venue_map_from_matched_pair(data: MatchedPair) -> dict[str, list[str]]:
    return {str(venue).upper(): list(ids) for venue, ids in data.venue_instrument_ids.items()}


def _venue_map_from_instrument_ids(instrument_ids) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for iid_str in sorted(str(iid) for iid in instrument_ids):
        iid = InstrumentId.from_str(iid_str)
        grouped.setdefault(iid.venue.value.upper(), []).append(iid_str)
    return grouped


class WebGatewayActor(Actor):
    def __init__(self, config: WebGatewayConfig, deps: WebGatewayDeps) -> None:
        super().__init__(config=config)
        self._host = config.host
        self._port = config.port
        self._loop = deps.loop
        self._risk_engine = deps.risk_engine          # 读 trading_state / live risk params
        self._arbitrage_params = deps.arbitrage_params or ArbitrageParams()
        self._config_path = deps.config_path           # PUT 写回 arb_config.json
        self._pair_registry = deps.pair_registry       # /odds + /matched_pairs 遍历用
        self._ws_clients: set[asyncio.Queue] = set()
        self._server: _NoSignalServer | None = None
        self._serve_task: asyncio.Task | None = None
        self._matched_pairs: dict[str, dict] = {}   # MatchedPair 元数据缓存;展示 membership 以 PairRegistry 为准

    # ── 生命周期 ──────────────────────────────────────────────────────
    def on_start(self) -> None:
        self._msgbus.subscribe(topic="events.risk", handler=self._on_risk_event)  # TradingStateChanged → WS 推
        self._msgbus.subscribe(topic=f"data.{MatchedPair.__name__}*", handler=self._on_matched_pair)  # Matching tab
        # 端口预检:uvicorn `serve()` 在 task 里跑,bind 失败(端口被占)会被 task 吞掉、不抛到节点,
        # 误导性的 "listening" 日志照样打。预检后端口被占就明确 error 并放弃启动。
        if not _port_bindable(self._host, self._port):
            self.log.error(
                f"WebGateway NOT started: {self._host}:{self._port} already in use "
                f"(改 config web.port 或释放该端口)",
            )
            return
        app = build_app(self)
        config = uvicorn.Config(app, host=self._host, port=self._port, log_level="warning", loop="none")
        self._server = _NoSignalServer(config)
        # 用**当前正在跑的** loop,而非注入的 `self._loop`:后者在 `add_actors`(node.run() 之前)捕获,
        # 可能不是节点实际运行的 loop,create_task 会落到一个永不运行的 loop 上 → serve() 不执行、
        # 不绑端口、也不报错("listening" 仍打,误导)。on_start 必在节点真 loop 上跑,get_running_loop 才对。
        self._loop = asyncio.get_running_loop()
        self._serve_task = self._loop.create_task(self._server.serve())
        self._serve_task.add_done_callback(self._on_serve_done)
        self.log.info(f"WebGateway listening on http://{self._host}:{self._port}")

    def _on_serve_done(self, task) -> None:
        """uvicorn serve() task 结束:正常停机静默,异常退出则 error(避免 bind 等失败被吞)。"""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.log.error(f"WebGateway server stopped with error: {exc!r}")

    def on_stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        for queue in list(self._ws_clients):  # 投毒丸让 WS 协程退出
            self._enqueue(queue, None)
        self._ws_clients.clear()

    # ── WS 广播 ───────────────────────────────────────────────────────
    def _broadcast(self, msg: dict) -> None:
        for queue in list(self._ws_clients):
            self._enqueue(queue, msg)

    @staticmethod
    def _enqueue(queue: asyncio.Queue, msg) -> None:
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()  # 丢最旧,塞最新
                queue.put_nowait(msg)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def register_ws(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_WS_QUEUE_MAXSIZE)
        self._ws_clients.add(queue)
        return queue

    def unregister_ws(self, queue: asyncio.Queue) -> None:
        self._ws_clients.discard(queue)

    # ── 控制台:TradingState 启停(读经引用,写经命令;web §8.1/§8.3)──────
    def _on_risk_event(self, event) -> None:
        state = getattr(event, "trading_state", None)  # TradingStateChanged
        if state is not None:
            self._broadcast({"type": "trading_state", "data": {"state": trading_state_to_str(state)}})

    def trading_state(self) -> str:
        """当前 TradingState 字符串(读 risk_engine 引用,始终准)。"""
        state = getattr(self._risk_engine, "trading_state", None)
        return trading_state_to_str(state) if state is not None else "UNKNOWN"

    def _publish(self, topic: str, msg) -> None:
        """publish 间接层(测试可在 bare 实例覆盖;NT `_msgbus` 是只读 cdef 属性不可在测试里替换)。"""
        self._msgbus.publish(topic=topic, msg=msg)

    def set_trading_state(self, state: str) -> None:
        """publish 启停命令(方案乙)。state ∈ {ACTIVE, HALTED}。"""
        self._publish(TOPIC_TRADING_STATE, SetTradingStateCommand(state=state))

    # ── 控制台:配置编辑(写回 arb_config.json + 热段发命令;web §8.2)──────
    def live_risk_params(self) -> dict:
        params = getattr(self._risk_engine, "_params", None)
        if params is None:
            return {}
        return {
            "match_tp": params.match_tp,
            "match_sl": params.match_sl,
            "min_probability": params.min_probability,
            "max_probability": params.max_probability,
        }

    def live_arbitrage_params(self) -> dict:
        params = self._arbitrage_params
        return {"share": params.share, "max_leg_share": params.max_leg_share, "fx": params.fx}

    def config_snapshot(self) -> dict:
        """当前生效配置:落盘文件内容 + 活值(trading_state + live risk/arbitrage params)。"""
        file_cfg: dict = {}
        if self._config_path is not None and Path(self._config_path).exists():
            file_cfg = json.loads(Path(self._config_path).read_text())
        return {
            "file": file_cfg,
            "live": {
                "trading_state": self.trading_state(),
                "risk": self.live_risk_params(),
                "arbitrage": self.live_arbitrage_params(),
            },
        }

    def update_config_section(self, section: str, fields: dict) -> dict:
        """写回 `arb_config.json` 的某段 + 热段额外 publish 命令。返回 {applied: live|on_restart}。

        热段:arbitrage(share/max_leg_share/fx)、risk(tp/sl/probability bounds)、
        matching/discovery(refresh_interval)。其余只落盘、需重启。
        """
        if self._config_path is None:
            raise RuntimeError("config_path 未注入,无法写配置")
        path = Path(self._config_path)
        cfg = json.loads(path.read_text()) if path.exists() else {}
        cfg.setdefault(section, {})
        if not isinstance(cfg[section], dict):
            raise ValueError(f"config section {section!r} 不是对象")
        cfg[section].update(fields)
        path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")

        applied = "on_restart"
        if section == "risk":
            self._publish(
                TOPIC_RISK_PARAMS,
                SetRiskParamsCommand(
                    match_tp=fields.get("match_tp"),
                    match_sl=fields.get("match_sl"),
                    min_probability=fields.get("min_probability"),
                    max_probability=fields.get("max_probability"),
                ),
            )
            applied = "live"
        elif section == "arbitrage":
            cmd = SetArbitrageParamsCommand(
                share=fields.get("share"),
                max_leg_share=fields.get("max_leg_share"),
                fx=fields.get("fx"),
            )
            self._publish(TOPIC_ARBITRAGE_PARAMS, cmd)
            overrides = {k: v for k, v in fields.items() if k in {"share", "max_leg_share", "fx"} and v is not None}
            if overrides:
                self._arbitrage_params = replace(self._arbitrage_params, **overrides)
            applied = "live"
        elif section in ("matching", "discovery") and "refresh_interval_secs" in fields:
            self._publish(TOPIC_REFRESH_INTERVAL, SetRefreshIntervalCommand(secs=float(fields["refresh_interval_secs"])))
            applied = "live"
        return {"status": "ok", "section": section, "applied": applied}

    # ── 只读快照:余额 + 赔率(纯读 cache,周期 GET)─────────────────────
    def accounts_snapshot(self) -> list[dict]:
        snapshot: list[dict] = []
        for account in self.cache.accounts():
            event = account.last_event
            if event is not None:
                snapshot.append(AccountState.to_dict(event))  # NT 原生 JSON-safe
        return snapshot

    def _on_matched_pair(self, data) -> None:
        if not isinstance(data, MatchedPair):
            return
        venue_instrument_ids = _venue_map_from_matched_pair(data)
        self._matched_pairs[data.pair_id] = {
            "pair_id": data.pair_id, "sport": data.sport, "competition": data.competition,
            "anchor_instrument_ids": list(data.anchor_instrument_ids),
            "tradable_instrument_ids": list(data.tradable_instrument_ids),
            "venue_instrument_ids": venue_instrument_ids,
            "confidence": data.confidence,
        }

    def matched_pairs(self) -> list[dict]:
        """Matching tab:当前已注册 pair;各 venue 的队名经 cache instrument.info 解析。"""
        reg = self._pair_registry
        if reg is None:
            return []
        out: list[dict] = []
        for pair_id in sorted(reg.all_pair_ids()):
            tradable_ids = sorted(reg.instrument_ids_for_pair(pair_id))
            anchor_ids = sorted(reg.anchor_ids_for_pair(pair_id))
            p = self._matched_pairs.get(pair_id, {})
            venue_instrument_ids = _venue_map_from_instrument_ids(tradable_ids)
            venue_teams = {
                venue: self._venue_teams(ids)
                for venue, ids in venue_instrument_ids.items()
            }
            out.append({
                "pair_id": pair_id,
                "sport": p.get("sport", ""),
                "competition": p.get("competition", ""),
                "anchor_instrument_ids": anchor_ids,
                "tradable_instrument_ids": tradable_ids,
                "venue_instrument_ids": venue_instrument_ids,
                "confidence": p.get("confidence", 0.0),
                "venue_teams": venue_teams,
            })
        return out

    def _venue_teams(self, iids: list[str]) -> str:
        """从该 venue 任一腿的 instrument.info 取 `home_team vs away_team`(各 venue 命名可能不同)。"""
        for iid_str in iids:
            inst = self.cache.instrument(InstrumentId.from_str(iid_str))
            info = getattr(inst, "info", None) or {}
            h, a = info.get("home_team"), info.get("away_team")
            if h and a:
                return f"{h} vs {a}"
        return ""

    def instruments_snapshot(self) -> list[dict]:
        """Discovery tab:cache instruments 按 (venue, 赛事) 去重的事件视图。"""
        seen: set[tuple] = set()
        out: list[dict] = []
        for inst in self.cache.instruments():
            info = getattr(inst, "info", None) or {}
            venue = inst.id.venue.value
            row = (venue, info.get("sport", ""), info.get("competition", ""),
                   info.get("home_team", ""), info.get("away_team", ""))
            if row in seen:
                continue
            seen.add(row)
            out.append({"venue": venue, "sport": row[1], "competition": row[2], "home": row[3], "away": row[4]})
        return out

    def odds_snapshot(self) -> list[dict]:
        """每场 matched pair 各腿的盘口最优价(读 PairRegistry + cache order book;无 firehose)。"""
        reg = self._pair_registry
        if reg is None:
            return []
        out: list[dict] = []
        for pair_id in sorted(reg.all_pair_ids()):
            legs: list[dict] = []
            for iid_str in sorted(reg.instrument_ids_for_pair(pair_id)):
                iid = InstrumentId.from_str(iid_str)
                book = self.cache.order_book(iid)
                inst = self.cache.instrument(iid)
                role = None
                if inst is not None and inst.info:
                    role = inst.info.get("selection_role") or inst.info.get("market_type")
                bid = book.best_bid_price() if book is not None else None
                ask = book.best_ask_price() if book is not None else None
                legs.append({
                    "venue": iid.venue.value,
                    "role": role,
                    "odds_model": descriptor_for(iid.venue.value).odds_model if is_known_venue(iid.venue.value) else "",
                    "bid": float(bid) if bid is not None else None,
                    "ask": float(ask) if ask is not None else None,
                })
            out.append({"pair_id": pair_id, "legs": legs})
        return out
