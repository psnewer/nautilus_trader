# Web 组件详细设计(Step 7 —— 只读监控 MVP)

> **状态**:只读监控 MVP 已落地(2026-06-21)。对应初设 `refactor.md §5.7`。
> **范围裁定(用户 2026-06-21)**:本轮只做**只读监控 + 后端 JSON/WS**,不碰交易路径;
> config-write(refresh_interval)、OrderBookDelta firehose、MessageBus request/response 桥**延后**(见 §7)。

---

## 1. 组件职责与边界

`WebGatewayActor`(`src/arbitrage/web/actor.py`)= NT `Actor` 子类,与 `TradingNode` **同进程同 asyncio loop**,
`on_start` 内拉起 FastAPI/uvicorn 协程。**纯观测面**:把 NT 运行态(账户/余额、matched_pairs、持仓 way_rebate)
转 JSON 经 HTTP GET + WebSocket 推浏览器。**替代旧 `BalanceMonitorActor`**:余额数字推前端,余额低/熔断
**由用户看着判断**,系统层不告警(对齐 risk §2.2「无 BalanceMonitorActor」)。

**边界(本 MVP 明确不做)**:
- ❌ 不发任何 NT 命令、不 publish、不写 cache —— 只读 `cache` / `portfolio`,对交易路径**透明**。
- ❌ 不做 config-write(HTTP POST→MessageBus→Actor),不碰 Refresher。
- ❌ 不订 `OrderBookDelta`(行情 firehose 量大,延后)。
- ❌ 不serve 前端 HTML/JS;只出 JSON/WS,前端/curl/浏览器自行消费。
- ❌ 不搬 legacy `services/web_gateway/`(3353 行,大部分绑老 pipeline 栈)。

**位置(P9)**:本类不 venue-coupled,住 `src/arbitrage/web/`(非 adapter 目录)。

## 2. 数据流

```
                    ┌──────────────── WebGatewayActor (NT loop) ─────────────────┐
events.account.* ──▶│ _on_account_state ─┐                                       │
data.MatchedPair* ─▶│ _on_matched_pair ──┼─▶ _broadcast(json) ─▶ 每个 WS client   │──▶ 浏览器 WS
                    │                     └─▶ _matched_pairs(dict 缓存)            │
                    │                                                            │
   HTTP GET ───────▶│ FastAPI 路由 ─▶ 读 cache.accounts() / portfolio.*(pull) ───│──▶ JSON resp
                    └────────────────────────────────────────────────────────────┘
```

- **推送面(WS)**:订阅事件 → 回调内 `put_nowait` 到每个已连 WS client 的 `asyncio.Queue` → client 协程 `get` 后 `send_json`。事件回调与 FastAPI 同 loop,无锁。
- **拉取面(HTTP GET)**:请求那一刻现读 `cache` / `portfolio`(pull-based,无中间缓存),与 risk §2.3 way_rebate pull 同风格。
- `matched_pairs` 是唯一缓存态:matching 周期 re-emit,Actor 累积进 dict(pair_id → 最近一份),供 GET 快照 + WS 增量。

## 3. 接口

### 3.1 Actor 骨架(`src/arbitrage/web/actor.py`)

```python
class WebGatewayConfig(ActorConfig, frozen=True, kw_only=True):
    enabled: bool = False
    host: str = "127.0.0.1"      # 默认只绑本机;暴露公网由用户显式改
    port: int = 8080

@dataclass(slots=True)
class WebGatewayDeps:            # 非 msgspec 对象经 deps 注入(同 matching/strategy 模式)
    portfolio: ArbitragePortfolio
    loop: asyncio.AbstractEventLoop

# ⚠️ NT `Actor.portfolio` 是只读 property,子类不能赋值 → 注入存为 `self._portfolio`
#   (对齐 StrategyEvaluator 约定);`app.py` 路由读 `actor._portfolio`。

class WebGatewayActor(Actor):
    def on_start(self):         # 订阅 events.account.* + data.MatchedPair*;create_task(server.serve())
    def on_stop(self):          # server.should_exit = True;断开所有 WS client
    def _on_account_state(self, event): ...   # → _broadcast({"type":"account", ...})
    def _on_matched_pair(self, data): ...      # 累积 _matched_pairs + _broadcast({"type":"matched_pair", ...})
    def _broadcast(self, msg: dict): ...        # put_nowait 到每个 WS client queue(满则丢该 client 最旧)
```

### 3.2 HTTP / WS 路由(`src/arbitrage/web/app.py:build_app(actor)`)

| 方法 | 路径 | 处理 | 用例 |
|---|---|---|---|
| GET | `/health` | `{"status":"ok"}` | 存活探针 |
| GET | `/accounts` | `cache.accounts()` → 每账户 balances 序列化(余额展示) | web-7.3(快照面) |
| GET | `/matched_pairs` | `_matched_pairs` 值列表 | web-7.1 |
| GET | `/positions/{pair_id}` | `portfolio.way_rebate(pair_id)` + `way_rebates_by_venue(pair_id)` + `outcome_exposures(pair_id)` | web-7.6 |
| GET | `/positions/global_min_rebate_sum` | `portfolio.global_min_rebate_sum()` → 数字 | web-7.7 |
| WS | `/ws` | 注册 client → 流式推 account / matched_pair JSON | web-7.3 / 7.1 增量 |

> **路由顺序**:`/positions/global_min_rebate_sum` 必须**先于** `/positions/{pair_id}` 注册,否则被路径参数吞掉。

### 3.3 消息接线(订阅 / 发布)

| 接收(subscribe) | 发布 | 不参与 |
|---|---|---|
| `events.account.{account_id}`(NT Portfolio 在 `update_account` 发,portfolio.pyx:477)→ `_on_account_state` | **无**(只读观测,不 publish、不写 cache) | 任何命令通路;不订 OrderBookDelta |
| `data.MatchedPair*`(MatchingActor `publish_data` 发,带 #58 尾 `*`)→ `_on_matched_pair` | | |

## 4. 关键机制

- **uvicorn 嵌入同 loop**:`on_start` 内 `uvicorn.Server(Config(app, host, port, log_level="warning", loop="none"))`,`self._loop.create_task(server.serve())`。**子类化 Server 把 `install_signal_handlers` no-op**——避免抢 NT 节点的信号处理(NT 自管 SIGINT/SIGTERM)。
- **端口预检 + 失败可见(2026-06-21 live 修)**:`serve()` 跑在 task 里,**bind 失败(端口被占)会被 task 吞掉、不抛到节点**,误导性的 "listening" 日志照样打(live 验证时撞到:8080 被本机 Java 占用,uvicorn 静默失败,curl 命中了那个 Java 服务)。修法两层:① `on_start` 先 `_port_bindable(host, port)` 同步探一次,占用则明确 `log.error` 并放弃启动(不打 "listening");② serve task `add_done_callback(_on_serve_done)`,异常退出则 `log.error`(兜住运行期崩溃)。
- **loop 必须用 `asyncio.get_running_loop()`(2026-06-21 live 修)**:`create_task(serve())` 必须落在节点**实际运行**的 loop 上。`WebGatewayDeps.loop` 是 `add_actors`(node.run() 之前)捕获的,可能不是真运行 loop —— 用它会把 serve task 排到一个永不运行的 loop 上,serve() 不执行、不绑端口、也不报错("listening" 照打,curl 连接被拒)。`on_start` 必在节点真 loop 上被调,故在 `on_start` 内 `self._loop = asyncio.get_running_loop()` 重取。
- **优雅停机**:`on_stop` 置 `server.should_exit = True`;uvicorn `serve()` 自然返回。同时给所有 WS client queue 投毒丸(`None`)让其协程退出。
- **WS 背压**:每 client 一个 `asyncio.Queue(maxsize=N)`;`_broadcast` 用 `put_nowait`,满则丢弃该 client 最旧一条(监控面允许丢帧,绝不能反压阻塞 NT 事件回调)。
- **只读安全**:Actor 只调 `cache` 读方法 + `portfolio` 的 pull 纯函数;无任何 NT 命令/写操作 → 不可能影响交易。即便 FastAPI 抛异常也被 uvicorn 吞在自己的 task 里,不波及 NT loop 上的交易回调。
- **enabled 门控**:`enabled=false`(默认)时 launcher **根本不构造/不 add** 本 Actor —— 零开销、零端口占用。

## 5. 与横切的咬合 / Q 映射

| 横切 | 约束 |
|---|---|
| Q14(way_rebate)| GET `/positions/*` 调 `ArbitragePortfolio` pull 方法(risk §4.1);只读,不触发重算副作用 |
| Q17 账户状态 | 余额真相由各 ExecutionClient 写 NT Cache;本 Actor 只读 `events.account.*` + `cache.accounts()`,对来源透明 |
| risk §2.2 | 替代 `BalanceMonitorActor`:推余额给前端,告警让用户自己看,系统层不熔断 |
| §6.10 同步 | 本 Actor 不参与健康检查 ⊥ 执行互斥;纯观测,无 await 循环阻塞交易 loop(WS 用非阻塞 queue) |

## 6. 落地清单(Step 7 MVP)

- [x] `WebGatewayConfig` / `WebGatewayDeps` / `WebGatewayActor`(`src/arbitrage/web/actor.py`)
- [x] `build_app(actor)` FastAPI 路由 + `/ws`(`src/arbitrage/web/app.py`)
- [x] `WebSectionConfig` 入 `ArbConfig` + `to_web_gateway_config` 派发 + 默认 `enabled=false`
- [x] launcher `add_actors`:`cfg.web.enabled` 时构造并 `add_actor`(注入 `node.kernel.portfolio` + loop)
- [x] 测试:`tests/arbitrage/web/`(FastAPI `TestClient` 测路由 + WS;Actor 用 stub portfolio/cache)
- [ ] **live 验证**:真节点起 + 浏览器/curl 看 `/accounts`、`/matched_pairs`、WS 流(待用户某次 live 跑顺带验)

## 7. 延后项(下一轮再议)

- **config-write**:HTTP POST `/config/refresh_interval` → publish `config.{venue}.refresh_interval` → Refresher 运行时生效(web-7.4,Q3)。需先定 Refresher 的命令订阅契约。
- **OrderBookDelta firehose**:订 `data.OrderBookDelta*` 转 JSON 推前端(web-7.2)。量大,需先定节流/采样策略。
- **MessageBus request/response 桥**:web-7.1 原设想经 request/response 拿数据;MVP 简化为 Actor 直接订阅 + 持 portfolio 引用,够用则不引入该桥。
- **前端**:静态 HTML/JS 面板(本 MVP 只出 JSON/WS)。
