# Web 组件详细设计(Step 7 —— 控制台)

> **状态**:**完整控制台页面**(忠实照搬 legacy Bootstrap 5 标签页:Market Discovery / Market Matching / Odds Monitor / Strategy / Configuration)已落地,详见 §8。对应初设 `refactor.md §5.7`。
> **范围演进**:① 只读监控 MVP(#118)→ ② 控制台(#119)→ ③ #120 一度移除监控只留控制面 → ④ **#123 用户要求照搬 legacy 完整页面,监控随页面重新加入**:`GET /`(serve HTML)+ 只读端点 `/accounts`(余额)、`/instruments`(发现仪表)、`/matched_pairs`(匹配表)、`/odds`(盘口,按 venue registry `odds_model` 前端换算成统一隐含概率)+ 控制台(启停 + 各 config 段编辑)。死面板/死字段(Run/Subscribe/pipeline、discount/global_sl/返水率面板)按用户裁定**删除**；#325 后市价行为改由 strategy JSON 的 `place_bets.params.market` 声明，Configuration 不再显示全局开关。本文 §1-7 = 通用骨架/机制,**控制语义真理源在 §8**。

---

## 1. 组件职责与边界

`WebGatewayActor`(`src/arbitrage/web/actor.py`)= NT `Actor` 子类,与 `TradingNode` **同进程同 asyncio loop**,
`on_start` 内拉起 FastAPI/uvicorn 协程。**控制面**:TradingState 启停 + 配置编辑(写经 MessageBus 命令、读经 `risk_engine` 引用)+ `/ws` 推 TradingState 变更。控制语义见 §8。

**边界**:
- ✅ **serve HTML 页面**(`GET /` 返 `static/console.html`,忠实照搬 legacy Bootstrap 标签页结构)。
- ✅ **只读监控**(纯读 cache/registry,周期 GET):`/accounts`(余额,读 `cache.accounts()`)、`/instruments`(读 `cache.instruments()` 去重事件视图)、`/matched_pairs`(订 `data.MatchedPair*` 累积)、`/odds`(读 `PairRegistry` + `cache.order_book` 最优价;无 firehose)。
- ✅ 控制:写经 MessageBus 命令(方案乙,§8.3);读经 `risk_engine` 引用 + 直接写 `arb_config.json`。
- ❌ **不订 `OrderBookDelta` firehose**(量大;`/odds` 用周期快照读 cache 盘口代替)。
- ❌ 不搬 legacy `services/web_gateway/` 代码(只照搬其 HTML 结构);pipeline start/stop、discovery/matching run、odds subscribe 在 NT 无意义 → 删。
- ❌ 不显示 legacy 死字段/死面板(discount/take_off/global_sl、返水率/持仓返水面板、全局市价开关)。市价由 strategy JSON 的 `place_bets.params.market` 按 Action 声明。

**位置(P9)**:本类不 venue-coupled,住 `src/arbitrage/web/`(非 adapter 目录)。

## 2. 数据流

```
                  ┌──────────────── WebGatewayActor (NT loop) ─────────────────┐
   events.risk ──▶│ _on_risk_event ─▶ _broadcast({type:trading_state}) ─▶ WS    │──▶ 浏览器 WS
                  │                                                            │
   HTTP POST ────▶│ /control/* /config/* ─▶ _publish(command.arb.*) ───────────│──▶ risk/matching apply
   HTTP GET  ────▶│ /control/trading_state /config ─▶ 读 risk_engine + 文件 ─────│──▶ JSON resp
                  └────────────────────────────────────────────────────────────┘
```

- **推送面(WS)**:只订 `events.risk`(`TradingStateChanged`)→ `put_nowait` 到每个 WS client `asyncio.Queue` → client 协程 `get` 后 `send_json`。同 loop,无锁。
- **控制写**:HTTP → `_publish` 到 `command.arb.*`,owner 组件(risk/strategy/matching)订阅 apply(§8.3)。
- **读**:`/control/trading_state` 读 `risk_engine.trading_state`;`/config` 读 `arb_config.json` 文件 + live risk params(`risk_engine._params`) + live arbitrage params(`WebGatewayActor._arbitrage_params`)。

## 3. 接口骨架

```python
class WebGatewayConfig(ActorConfig, frozen=True, kw_only=True):
    enabled: bool = False
    host: str = "127.0.0.1"            # 默认只绑本机;暴露公网由用户显式改
    port: int = 8080
    start_halted: bool = True          # boot 即 HALTED(§8.1)

@dataclass(slots=True)
class WebGatewayDeps:                   # 非 msgspec 对象经 deps 注入
    loop: asyncio.AbstractEventLoop
    risk_engine: object | None = None   # 读 trading_state / live risk params(写走命令)
    arbitrage_params: ArbitrageParams | None = None  # 读/热改 share/max_leg_share/fx/evaluate_on_depth_change
    config_path: str | None = None      # arb_config.json,PUT 写回

class WebGatewayActor(Actor):
    def on_start(self):     # 订 events.risk;get_running_loop + create_task(server.serve())
    def on_stop(self):      # server.should_exit = True;WS client 投毒丸
    def _on_risk_event(self, e): ...     # TradingStateChanged → _broadcast({type:trading_state})
    # 控制方法见 §8:trading_state() / set_trading_state() / config_snapshot() / update_config_section()
```

**路由**:`GET /health` + 控制台路由(`/control/trading_state`、`/config`、`/ws`)见 §8.4。

**消息接线**:接收 `events.risk`(NT RiskEngine set_trading_state 时发);发布 `command.arb.{trading_state,risk_params,arbitrage_params,refresh_interval}`(§8.3);不订任何监控/行情 topic。

## 4. 关键机制(scaffolding)

- **uvicorn 嵌入同 loop**:`on_start` 内 `uvicorn.Server(Config(app, host, port, log_level="warning", loop="none"))`,`self._loop.create_task(server.serve())`。**子类化 Server 把 `install_signal_handlers` no-op**——避免抢 NT 节点的信号处理(NT 自管 SIGINT/SIGTERM)。
- **端口预检 + 失败可见(2026-06-21 live 修)**:`serve()` 跑在 task 里,**bind 失败(端口被占)会被 task 吞掉、不抛到节点**,误导性的 "listening" 日志照样打(live 验证时撞到:8080 被本机 Java 占用,uvicorn 静默失败,curl 命中了那个 Java 服务)。修法两层:① `on_start` 先 `_port_bindable(host, port)` 同步探一次,占用则明确 `log.error` 并放弃启动(不打 "listening");② serve task `add_done_callback(_on_serve_done)`,异常退出则 `log.error`(兜住运行期崩溃)。
- **loop 必须用 `asyncio.get_running_loop()`(2026-06-21 live 修)**:`create_task(serve())` 必须落在节点**实际运行**的 loop 上。`WebGatewayDeps.loop` 是 `add_actors`(node.run() 之前)捕获的,可能不是真运行 loop —— 用它会把 serve task 排到一个永不运行的 loop 上,serve() 不执行、不绑端口、也不报错("listening" 照打,curl 连接被拒)。`on_start` 必在节点真 loop 上被调,故在 `on_start` 内 `self._loop = asyncio.get_running_loop()` 重取。
- **优雅停机**:`on_stop` 置 `server.should_exit = True`;uvicorn `serve()` 自然返回。同时给所有 WS client queue 投毒丸(`None`)让其协程退出。
- **WS 背压**:每 client 一个 `asyncio.Queue(maxsize=N)`;`_broadcast` 用 `put_nowait`,满则丢弃该 client 最旧一条(允许丢帧,绝不能反压阻塞 NT 事件回调)。
- **写安全**:控制写经 MessageBus 命令、组件侧校验(§8.5);读经 risk_engine 引用 + 文件;FastAPI 异常被 uvicorn 吞在自己的 task 里,不波及 NT loop 上的交易回调。
- **enabled 门控**:`enabled=false`(默认)时 launcher **根本不构造/不 add** 本 Actor —— 零开销、零端口占用。

## 5. 与横切的咬合

| 横切 | 约束 |
|---|---|
| Q16 TradingState(修订)| 控制台启停显式 `set_trading_state`(人工熔断);自动门控仍不碰 TradingState(risk §4.3 前向指针)。详见 §8.1 |
| Q17 账户状态 | 余额真相仍由各 ExecutionClient 写 NT Cache;`/accounts`(#123)只读 `cache.accounts()` 序列化,navbar 显示余额数字,余额低/熔断由用户看着判断(替代旧 BalanceMonitorActor)|
| Venue registry | 页面展示不再把 `POLYMARKET` 写死为唯一主腿;Discovery 统计/过滤从 `/instruments` 实际 venue 动态生成;Matching/Odds 的列名按 `config.venues.*.enabled` 中的可交易 venue 生成,列标题即真实 venue id,不展示 PMSPORTS/anchor;Matching 表分组只读 `venue_instrument_ids`,不再输出旧 `pm_*` / `oe_*` 字段;`/odds` leg 携带 `odds_model` 供前端决定是否 `1/decimal_odds` 换算 |
| §6.10 同步 | 本 Actor 不参与健康检查 ⊥ 执行互斥;无 await 循环阻塞交易 loop(WS 用非阻塞 queue) |

## 6. 落地清单(scaffolding;控制台清单见 §8.6)

- [x] `WebGatewayConfig`(+`start_halted`)/ `WebGatewayDeps`(loop/risk_engine/config_path)/ `WebGatewayActor`
- [x] `build_app(actor)` `/health` + `/ws` + 控制路由(`app.py`)
- [x] `WebSectionConfig` 入 `ArbConfig` + `to_web_gateway_config` + 默认 `enabled=false`
- [x] launcher `add_actors`:`cfg.web.enabled` 时构造(注入 risk_engine + loop + config_path)
- [x] uvicorn 嵌入 / 端口预检 / get_running_loop / 优雅停机(§4)+ 测试

## 7. 演进 / 延后

- **#120 一度移除、#123 又加回**:监控 endpoint 在 #120 被裁掉(web 只留控制面),#123 用户要求照搬 legacy 完整页面时**重新加入**:`/accounts`(余额)、`/instruments`(发现)、`/matched_pairs`(匹配)、`/odds`(盘口)+ `GET /` serve HTML。`/positions/{pair_id}` way_rebate 端点**不恢复**(way_rebate 已 #121 退役)。
- **删除(NT 无对应或已迁为订单级)**:legacy 的 Run Discovery/Matching、Subscribe Odds、pipeline start/stop;Execution 的 discount/take_off 和全局/venue-local market-order 字段;Risk 的 global_sl、健康检查间隔、返水率/持仓返水状态面板。市价由 `place_bets.params.market` 声明。
- **延后**:OrderBookDelta firehose 实时推(量大;现用 `/odds` 周期快照);strategy 的可视化 Condition 树编辑(现走 strategy JSON 原始编辑)。

---

## 8. 控制台 —— TradingState 启停 + 配置编辑(Step 7 控制面,2026-06-21 定)

> **范围裁定(用户 2026-06-21)**:legacy 控制台的 pipeline start/stop、discovery/matching `run`、odds subscribe 等**在 NT 里无意义**(发现/匹配/订阅均连续自动),全部**不做**。控制台只做两件 NT 里成立的事:**① TradingState 启停**、**② 配置编辑(C 混合:热改 + 重启)**。决策史见 refactor.md 修订记录 #119。

### 8.1 启停按钮 = NT 原生 TradingState

- **复用 NT 原生 `RiskEngine.set_trading_state(ACTIVE/HALTED)`**(`risk/engine.pyx:229`),**不用 REDUCING**。HALTED 在 egress `_execution_gateway`→`_deny_command`→`_deny_order` 拦所有新 submit(`risk/engine.pyx:1124`)。**barrier 安全**:`_deny_command(SubmitOrder)` 走 `_deny_order`,而 `ArbitrageLiveRiskEngine._deny_order` 已重写(发 `risk.opportunity.leg_denied`)→ HALTED 时 opportunity barrier 正常释放 `pair_inflight`,不泄漏。
- **boot 默认 HALTED**:NT `RiskEngine.__init__` 默认 ACTIVE(`engine.pyx:133`);本系统 launcher 在 `node.build()` 后、`node.run()` 前对 `node.kernel.risk_engine.set_trading_state(HALTED)`。**仅当 `web.enabled` 且 `web.start_halted`(默认 true)时生效** —— web 关闭时无按钮可解除,保持 NT 原生 ACTIVE,否则节点永不交易。
- **不联动 strategy 评估(用户 2026-06-21)**:Stop 只切 HALTED,strategy 继续评估、submit 在 egress 被拒。**已知取舍**:HALTED 期间若有机会信号会刷 `DENIED: TradingState.HALTED` 警告(churn)。接受;若日后噪声大再议联动。
- ⚠️ **对 Q16「不主动改 TradingState」的修订**:risk §4.3/§4.4 锁定的是**自动门控**(profit gates / venue liveness)不用 TradingState、走逐 submit deny;**本节是人工操作员熔断**,二者正交并存——自动门控仍不碰 TradingState,只有控制台启停按钮显式 set。

### 8.2 配置编辑(C 混合:热改 + 重启)

每次保存都**写回 `arb_config.json`**(对齐 legacy `_save_config`,重启不丢);其中"热字段"额外推命令给活节点即时生效,"重启字段"只落文件 + 页面标"需重启"。

| 段 | 字段 | 生效方式 |
|---|---|---|
| arbitrage | `share` / `max_leg_share` / `fx` / `evaluate_on_depth_change` | **热改** → `command.arb.arbitrage_params` |
| risk | `match_tp` / `match_sl` / `min_probability` / `max_probability` / `prob_buy_only` | **热改** → `command.arb.risk_params` |
| matching/discovery | `refresh_interval` | **热改** → `command.arb.refresh_interval` |
| venues | 凭证 / URL | **重启**(连接态,结构性) |
| discovery | competitions / sports | **重启**(要 provider 重载 instruments) |
| web / execution | host/port / 超时 | **重启** |

`evaluate_on_depth_change` 与 `prob_buy_only` 必须是 JSON boolean；WebGatewayActor 在写回文件和
publish 命令**之前**校验，非 boolean 返错且不污染落盘配置。Strategy/Risk consumer 对直接
注入的非 boolean 命令亦 fail-closed 拒绝对应整次 params 更新。

Execution 配置页不提供全局市价开关。需要市价执行的树在 strategy JSON 中配置
`place_bets.params.market=true`；最终转换规则见 execution §3.6，WebGateway 不参与改价。

Discovery 配置页展示约定:
- `discovery.polymarket/orbitexch/sharpexch.sports` 通过 Polymarket / OrbitExch / SharpExch 三个标签页分别编辑。
- PMSPORTS 的 sports status data source 暂不在页面单独显式配置;默认 `data_sources.sports_status.sports` 为空,dispatcher 继承 `discovery.polymarket.sports` 作为 PMSPORTS discovery / sports firehose 目标过滤。
- `page_load_timeout_sec` / `staleness_timeout_sec` 是 external browser discovery 的统一 UI 值,不在页面上区分 OE/SE;保存时同步写入 `venues.orbitexch` 与 `venues.sharpexch` 的同名字段,以兼容现有 schema/dispatcher。

### 8.3 接线 seam = MessageBus 命令(方案乙,解耦)

WebGatewayActor **不直接调引擎方法**;它 publish 控制命令,**各 owner 组件订阅自行 apply**(producer=web 定义契约,consumer=risk/matching;P11 单一生产者归属 → 契约住 web 组件,consumer 交叉引用)。

| 命令 topic | payload | 消费者 | apply |
|---|---|---|---|
| `command.arb.trading_state` | `{"state": "ACTIVE"\|"HALTED"}` | `ArbitrageLiveRiskEngine`(`configure_arb` 内 subscribe)| `self.set_trading_state(...)` |
| `command.arb.risk_params` | risk 字段 dict(`match_tp`/`match_sl`/概率上下界/`prob_buy_only`;None=不动) | `ArbitrageLiveRiskEngine` | 校验并覆盖给定 `self._arb_params` 字段 |
| `command.arb.arbitrage_params` | arbitrage 字段 dict(`share`/`max_leg_share`/`fx`/`evaluate_on_depth_change`;None=不动) | `ArbitrageLiveRiskEngine` / `StrategyEvaluator` | Risk 保持共享 `ArbitrageParams` 副本，但不消费深度开关；StrategyEvaluator 用规模字段构造 `strategy_defaults`，用 `evaluate_on_depth_change` 过滤 OBD 调度 |
| `command.arb.refresh_interval` | `{"secs": float}` | `MarketMatchingActor`(`on_start` 内 subscribe)| 更新 `self._refresh_interval_secs` |

命令消息类型 + topic 常量住 `src/arbitrage/common/control.py`(轻 frozen dataclass)。

### 8.4 路由(扩 `app.py`)

| 方法 | 路径 | 处理 |
|---|---|---|
| GET | `/` | serve `static/console.html`(legacy 风格标签页)|
| GET | `/health` | `{"status":"ok"}` —— **web server 存活探针,非交易健康检查**(#109/#110 退役的 PM/OE HealthCheckLoop 与此无关)|
| GET | `/control/trading_state` | 当前 TradingState(读 `risk_engine.trading_state`)|
| POST | `/control/trading_state` | body `{state}` → publish `command.arb.trading_state`;Halt→Active 前端二次确认 |
| GET | `/config` | 当前生效配置快照(file `arb_config.json` + live risk params + live arbitrage params)|
| PUT | `/config/{section}` | 校验 → 写回 `arb_config.json`;热段额外 publish 对应命令;重启段返回 `{"applied":"on_restart"}` |
| GET | `/accounts` | `cache.accounts()` 序列化(余额)|
| GET | `/instruments` | cache instruments 去重事件视图(发现表)|
| GET | `/matched_pairs` | PairRegistry 当前注册 pair(匹配表);`data.MatchedPair*` 只缓存 `sport`/`competition`/`confidence` 等元数据,页面 membership 不再按事件累积。输出 `venue_instrument_ids` / `tradable_instrument_ids` / `anchor_instrument_ids` / `venue_teams` / `confidence`,不再输出旧 `pm_*` / `oe_*` / `external_*` 字段;`tradable_instrument_ids` / `anchor_instrument_ids` 来自 PairRegistry 当前态,因此 ended eviction / `unregister_pair()` 后页面同步消失;Web 展示列按配置 enabled tradable venues 生成,每列从 `venue_teams[venue]` 取值,不用旧 PM/OE 字段或 instrument id 后缀拼接兜底,也不显示 anchor/Confidence 列 |
| GET | `/odds` | PairRegistry + `cache.order_book` 最优价;每条 leg 带 `venue/role/claim/quote_claim/odds_model/bid/ask`。前端按 `claim` 分 yes/no 行；decimal 普通真实腿按 `1/odds`,只有 `quote_claim=no` 的合成 lay 行情按 `1−1/odds`。3-way 仍是一场三个 role pair,每个 pair 两行 |
| WS | `/ws` | 推 `TradingStateChanged`(订 `events.risk`)|

### 8.5 安全

- `host` 默认 `127.0.0.1` 本机;**boot 默认 HALTED** 兜底(误暴露也 boot 即停)。
- 写操作 MVP 不加鉴权(靠 localhost 绑定);Start(Halt→Active)UI 二次确认。
- 真金边界:所有热改写的是活节点真账户;`command.arb.*` 仅 web→组件单向,组件侧校验 payload(非法值拒绝并 log,不 apply)。

### 8.6 落地清单(控制台)

- [x] `src/arbitrage/common/control.py`:命令类型 + topic 常量
- [x] `ArbitrageLiveRiskEngine.configure_arb`:subscribe `command.arb.trading_state` + `command.arb.risk_params` + `command.arb.arbitrage_params`
- [x] `StrategyEvaluator.on_start`:subscribe `command.arb.arbitrage_params`
- [x] `MarketMatchingActor.on_start`:subscribe `command.arb.refresh_interval`
- [x] launcher:`web.enabled && web.start_halted` → build 后 `risk_engine.set_trading_state(HALTED)`;`WebSectionConfig` 加 `start_halted`
- [x] `app.py` 路由 + `actor.py` publish 命令 / 写 `arb_config.json`(注入 cfg 快照 + 配置文件路径)
- [x] 测试:命令 publish/consume apply、boot HALTED、PUT 写文件 + 热段发命令、HALTED deny 经 barrier 释放。详见 `tests/arbitrage/web/README.md` web-7.8~7.12、risk hot-update、matching refresh interval 与 launcher boot HALTED 用例。
- [ ] **live 验证**:真节点 boot HALTED → 点 Start 转 ACTIVE → 下单放行;改 arbitrage/risk 参数热生效
