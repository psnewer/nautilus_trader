# config 测试

对应章节: `refactor.md #41`(Q22-Q27 锁定);详细设计 `architectures/_cross-cutting/configuration.md`。

2026-07-23 #271 后，legacy WebGateway `default_config.json` 及管理脚本已删除；
运行配置唯一入口是 `arb_config.json` + env 凭证注入。

## Slice 3 落地(2026-05-28 #42)

ArbConfig schema(msgspec)+ JSON loader + env 凭证注入。

- ✅ `test_loader.py`(57):JSON 解析 / 默认值 / config 包导出当前 schema 类型 / `arb_config.example.json` 不写 `data_sources` 但获得 PMSPORTS 默认值 / 未知字段统一 schema mismatch(含旧 `risk.share/max_leg_share/fx`、`risk.global_sl`、risk 健康/override 死字段、OE/SE 旧执行/浏览器/API 死字段、execution 旧 session/staleness 字段、PM 旧地址字段、旧 `strategy.signals`、无效顶层 enabled 开关) / 凭证 env 注入 / PM proxy env 注入(JSON 显式值优先) / env 缺失保 None / 凭证-JSON ConfigWarning / 错误路径(文件不存在 / 无效 JSON / schema 不匹配 / `venues` 或单 venue section 显式非 object)

## Slice 4 落地(2026-05-28 #43)

`ArbConfig` → 各组件 config 的纯函数派发。

- ✅ `test_dispatcher.py`:PM/OE Data/Exec Client config 凭证映射 / PM `ws_url` 只接受 base URL(`/ws` 或 `/ws/`),拒绝 channel endpoint(`/ws/market` / `/ws/user`) / PMSPORTS `data_sources.sports_status.ws_url` 透传 / PM `proxy_url` 透传(WS + #98 CLOB REST factory + #111 Data API `/positions` async client 消费) / PM Exec retry 参数显式透传且默认 None / OE `page_load_timeout_sec` → Data/Exec `page_timeout` / **OE `venues.orbitexch.staleness_timeout_sec` → `OrbitExchDataClientConfig.staleness_timeout_secs`**(#109:WS handler 内部 liveness timeout,默认 300s;旧 `health_interval_sec`/HealthCheckLoop + `health_check_exec_reload_enabled` 均退役删除)/ SE `headless`、`browser_type`、`user_data_dir` 映射到 SE Data/Exec config，`cloudflare_timeout_sec` 仅映射 Exec 登录等待，生产复用 profile 需显式配置 `venues.sharpexch.user_data_dir` / PM 凭证 None passthrough / OE 凭证缺省转空串 / MarketMatching competition_max_matches(含 empty → None) + `anchor_venue=PMSPORTS` + `tradable_venues` 从 Venue Registry enabled helpers 派生,OE+SE-only 时 anchor 仍为 PMSPORTS / StrategyEvaluator log_evaluations 默认值 + `strategy.log_evaluations` 映射 / ArbRiskParams 风控字段 / ArbitrageParams 普通运行默认值(`share/max_leg_share/fx`) / ArbContext init kwargs(execution → enabled trading venue 的 `session_timeout_secs_by_venue`;OE/SE discovery → `discovery_config_by_venue`;alias → enabled trading venue 的 `*_aliases_by_venue`;**PM/OE/SE discovery context 经 Venue Registry descriptor `config_key` 同时受 runtime venue enabled 与 `discovery.<venue>.enabled` 控制;PMSPORTS target competitions 可由 `data_sources.sports_status.sports` 覆盖并同步写入 `target_competitions_by_data_source`,常规省略时继承 `discovery.polymarket.sports`,且不受 PM trading venue enabled 影响**;**#109/#110:PM/OE 健康 interval 死接线均已删,断言 `pm_health_interval_secs`/`oe_health_interval_secs` 不出现**)/ Debug None when missing/disabled / Debug enabled overrides+mock_data / Debug conditions 不匹配返 None / 纯函数不 mutate cfg

## Slice 5 落地(2026-05-28 #44)

`to_strategy_registry(cfg)` 增补 dispatcher(消费 ArbConfig.strategy 经 `build_strategy_registry`);
`strategy.enabled=false` 时返回空 registry,保留 StrategyEvaluator 的 MatchedPair→OBD 订阅桥但不触发 Action。
本身的 Strategy JSON 递归解析测试在 **`tests/arbitrage/strategy/test_json_loader.py`**(strategy capability 域):BoolExpr / Condition / Check / Action registry / binding / strategy disabled / unknown type fail-fast。

## 后续 slice 落地状态

- ✅ **slice 6**:`launchers/arb_node.py` 接线已落地。`tests/arbitrage/launchers/test_arb_node.py` 覆盖 config → factories / StrategyEvaluator / WebGatewayActor / boot HALTED 等 wiring。
- ✅ **slice 7**:OE data factory 真接 scraper/provider 已落地。`tests/arbitrage/adapters/orbitexch/test_data_factory_provider_wiring.py` 覆盖 factory 从 venue keyed context 读取 discovery config、alias、fx,并回写 `instrument_provider_by_venue["ORBITEXCH"]`。
- ✅ **slice 8**:运行组件接线已落地。`MarketMatchingActor` / `StrategyEvaluator` NT Strategy /
  optional `WebGatewayActor` 由 launcher 构造，运行时控制命令由 owner 组件订阅 apply；对应验收分布在
  launcher、matching、strategy、web 测试域。

## 关键约束(P10-类同 / Q23)

**凭证字段唯一通道是 env**;loader 检测到 JSON 含凭证字段非空时发 `ConfigWarning`。env 缺失不 raise(下游 client 构造时按需 raise)。

## 预期用例

### config-loader.{1-x}
- **.1**:default JSON(全字段省略)→ ArbConfig 默认值
- **.2**:JSON 全字段 → 全部解析
- **.3**:env 凭证注入 PM 字段 → cfg.venues.polymarket.* 取 env 值
- **.3b**:PM proxy JSON 缺省时从 `POLYMARKET_PROXY_URL` / 系统 proxy env 注入;JSON 显式 `proxy_url` 优先
- **.4**:env 凭证注入 OE 字段 → cfg.venues.orbitexch.* 取 env 值
- **.5**:env 缺失 → cfg 字段保 None,不 raise
- **.7**:env 优先于 JSON 内的同字段
- **.8**:凭证存在 JSON 中(非 None / 非空)→ ConfigWarning
- **.9**:JSON 凭证段省略 → 无 warning(干净路径)
- **.10**:文件不存在 → ConfigError
- **.11**:无效 JSON → ConfigError
- **.12**:schema 字段类型错 → ConfigError
- **.13**:`venues` 段省略 → loader 补默认 section,env 注入仍生效
