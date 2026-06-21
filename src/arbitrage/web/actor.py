"""
WebGatewayActor —— Step 7 只读监控网关(NT `Actor` 子类,与 TradingNode 同进程同 loop)。

`on_start` 内拉起 FastAPI/uvicorn 协程(`build_app` 路由),订阅 `events.account.*` +
`data.MatchedPair*` 转 JSON 经 WS 推浏览器;HTTP GET 现读 `cache` / `portfolio`(pull)。
**纯观测面:只读,不发命令、不 publish、不写 cache** —— 对交易路径透明。

详细设计:`docs/arbitrage/architectures/web/architecture.md`。
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass

import uvicorn

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.actor import ActorConfig
from nautilus_trader.model.events import AccountState

from src.arbitrage.matching.events import MatchedPair
from src.arbitrage.risk.portfolio import ArbitragePortfolio
from src.arbitrage.web.app import build_app


_WS_QUEUE_MAXSIZE = 256  # 每 client 队列上限;满则丢最旧(监控面允许丢帧,绝不反压交易回调)


class WebGatewayConfig(ActorConfig, frozen=True, kw_only=True):
    enabled: bool = False
    host: str = "127.0.0.1"  # 默认只绑本机;暴露公网需显式改
    port: int = 8080


@dataclass(slots=True)
class WebGatewayDeps:
    """非 msgspec 对象经 deps 注入(同 matching/strategy 模式)。"""

    portfolio: ArbitragePortfolio
    loop: asyncio.AbstractEventLoop


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


def _matched_pair_to_json(data: MatchedPair) -> dict:
    return {
        "pair_id": data.pair_id,
        "sport": data.sport,
        "competition": data.competition,
        "pm_instrument_ids": list(data.pm_instrument_ids),
        "oe_instrument_ids": list(data.oe_instrument_ids),
        "confidence": data.confidence,
        "ts_event": data.ts_event,
    }


def _account_state_to_json(event: AccountState) -> dict:
    return AccountState.to_dict(event)  # NT 原生 JSON-safe 序列化


class WebGatewayActor(Actor):
    def __init__(self, config: WebGatewayConfig, deps: WebGatewayDeps) -> None:
        super().__init__(config=config)
        self._host = config.host
        self._port = config.port
        # NT `Actor.portfolio` 是只读 property → 用 `_portfolio`(对齐 StrategyEvaluator 约定);app 路由读它。
        self._portfolio = deps.portfolio
        self._loop = deps.loop
        self._matched_pairs: dict[str, dict] = {}
        self._ws_clients: set[asyncio.Queue] = set()
        self._server: _NoSignalServer | None = None
        self._serve_task: asyncio.Task | None = None

    # ── 生命周期 ──────────────────────────────────────────────────────
    def on_start(self) -> None:
        self._msgbus.subscribe(topic="events.account.*", handler=self._on_account_state)
        self._msgbus.subscribe(topic=f"data.{MatchedPair.__name__}*", handler=self._on_matched_pair)
        # 端口预检:uvicorn `serve()` 在 task 里跑,bind 失败(端口被占)会被 task 吞掉、不抛到节点,
        # 误导性的 "listening" 日志照样打。预检后端口被占就明确 error 并放弃启动(不订阅已完成,无害)。
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

    # ── 事件回调 → 广播 ───────────────────────────────────────────────
    def _on_account_state(self, event) -> None:
        if not isinstance(event, AccountState):
            return
        self._broadcast({"type": "account", "data": _account_state_to_json(event)})

    def _on_matched_pair(self, data) -> None:
        if not isinstance(data, MatchedPair):
            return
        payload = _matched_pair_to_json(data)
        self._matched_pairs[data.pair_id] = payload
        self._broadcast({"type": "matched_pair", "data": payload})

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

    # ── WS client 注册(app 调用)─────────────────────────────────────
    def register_ws(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_WS_QUEUE_MAXSIZE)
        self._ws_clients.add(queue)
        return queue

    def unregister_ws(self, queue: asyncio.Queue) -> None:
        self._ws_clients.discard(queue)

    # ── 快照(app GET 路由读)─────────────────────────────────────────
    def matched_pairs(self) -> list[dict]:
        return list(self._matched_pairs.values())

    def accounts_snapshot(self) -> list[dict]:
        snapshot: list[dict] = []
        for account in self.cache.accounts():
            event = account.last_event
            if event is not None:
                snapshot.append(_account_state_to_json(event))
        return snapshot
