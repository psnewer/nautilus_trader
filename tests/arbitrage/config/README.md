# config 测试

对应章节: `refactor.md #41`(Q22-Q27 锁定);详细设计 `architectures/_cross-cutting/configuration.md`。

## Slice 3 落地(2026-05-28 #42)

ArbConfig schema(msgspec)+ JSON loader + env 凭证注入。

- ✅ `test_loader.py`(15):JSON 解析 / 默认值 / 凭证 env 注入 / env 缺失保 None / 别名 fallback / 凭证-JSON ConfigWarning / 错误路径(文件不存在 / 无效 JSON / schema 不匹配)

## Slice 4 落地(2026-05-28 #43)

`ArbConfig` → 各组件 config 的纯函数派发。

- ✅ `test_dispatcher.py`(17):PM/OE Data/Exec Client config 凭证映射 / PM 凭证 None passthrough / OE 凭证空串 fallback / InstrumentRefresher (PM,OE) pair / MarketMatching min_similarity + competition_max_matches (含 empty → None 兜底)/ StrategyEvaluator log_evaluations / ArbRiskParams 全字段 / ArbContext init kwargs(execution → PM+OE session/health)/ Debug None when missing/disabled / Debug enabled overrides+mock_data / Debug conditions 不匹配返 None / 纯函数不 mutate cfg

## Slice 5 落地(2026-05-28 #44)

`to_strategy_registry(cfg)` 增补 dispatcher(消费 ArbConfig.strategy 经 `build_strategy_registry`);
本身的 Strategy JSON 解析测试在 **`tests/arbitrage/strategy/test_json_loader.py`**(strategy capability 域)。

**仍待**(后续 slice):
- ⬜ slice 5:Strategy JSON parser(BoolExpr/Condition `from_json` 递归 + Check/Action registry)
- ⬜ slice 6:`launchers/arb_node.py` 接线
- ⬜ slice 7:OE data factory 真接 scraper
- ⬜ slice 8:Actors 接线

## 关键约束(P10-类同 / Q23)

**凭证字段唯一通道是 env**;loader 检测到 JSON 含凭证字段非空时发 `ConfigWarning`。env 缺失不 raise(下游 client 构造时按需 raise)。

## 预期用例

### config-loader.{1-x}
- **.1**:default JSON(全字段省略)→ ArbConfig 默认值
- **.2**:JSON 全字段 → 全部解析
- **.3**:env 凭证注入 PM 字段 → cfg.venues.polymarket.* 取 env 值
- **.4**:env 凭证注入 OE 字段 → cfg.venues.orbitexch.* 取 env 值
- **.5**:env 缺失 → cfg 字段保 None,不 raise
- **.6**:`POLYMARKET_ADDRESS` 别名 fallback for `POLYMARKET_USER_ADDRESS`
- **.7**:env 优先于 JSON 内的同字段
- **.8**:凭证存在 JSON 中(非 None / 非空)→ ConfigWarning
- **.9**:JSON 凭证段省略 → 无 warning(干净路径)
- **.10**:文件不存在 → ConfigError
- **.11**:无效 JSON → ConfigError
- **.12**:schema 字段类型错 → ConfigError
- **.13**:`venues` 段省略 → loader setdefault 兜底不 crash
