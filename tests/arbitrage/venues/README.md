# Venue Registry 测试计划

对应详细设计:`docs/arbitrage/architectures/_cross-cutting/venues.md`。

## venues-1:静态 descriptor

**前置**:代码侧 `VENUE_REGISTRY` 已落地。
**输入**:读取 registry。
**期望**:`POLYMARKET` 为 `odds_model=probability`;`ORBITEXCH` 与
`SHARPEXCH` 为 `odds_model=decimal`;SE/OE 保留真实 venue id,并在 descriptor 上声明各自的
`discovery_config_builder`。PMSPORTS matching anchor 由 data source descriptor 表达,不由
venue descriptor 的展示字段表达。
**验收**:已落地。`tests/arbitrage/common/test_venues.py` 覆盖静态 descriptor。

## venues-2:enabled venue 派生

**前置**:构造 PM+OE、PM+SE、PM+OE+SE 三种 `ArbConfig`。
**输入**:`is_venue_enabled(cfg, venue)` / `enabled_venues(cfg)` /
`enabled_tradable_venues(cfg)`。
**期望**:只返回配置启用的 descriptor;tradable 当前等价于 enabled runtime venue 且不包含 PMSPORTS。
**验收**:已落地。`tests/arbitrage/common/test_venues.py` 覆盖 PM+SE enabled 派生,并覆盖
PMSPORTS data source 与 PM trading venue enablement 解耦。

## venues-3:launcher 注册

**前置**:fake `TradingNode` 记录 add factory 调用。
**输入**:三种 enabled 配置。
**期望**:PM+SE 不注册 OE;PM+OE 不注册 SE;OE+SE 不注册 PM 但仍注册 PMSPORTS;
PM+OE+SE 三家均注册;PMSPORTS sports anchor 由 `data_sources.sports_status` 注册。
**验收**:已落地。`tests/arbitrage/launchers/test_arb_node.py` 覆盖 launcher factory 注册。

## venues-4:validation

**输入**:少于两个 enabled venue、`data_sources.sports_status.enabled=false`。
**期望**:少于两个 venue 报 `ConfigError`;缺 PMSPORTS data source 报 `ConfigError`;
`data_sources.sports_status.provider` 写错时也按缺 PMSPORTS data source 报错;OE+SE-only 在配置/launcher 层可通过。
**验收**:已落地。`tests/arbitrage/launchers/test_arb_node.py` 覆盖少于两个 venue 与缺 PMSPORTS provider。

## venues-5:strategy helper

**输入**:PM price、OE/SE decimal odds、PM/OE/SE 同概率 tie-break。
**期望**:`probability_from_price` 与 `qty_from_share` 对 PM 使用 probability/share 公式,
对 OE/SE 使用 decimal odds 公式;`venue_preference_rank` 保证同概率时 probability venue 先于 decimal venue,
再按 registry 顺序稳定排序;调用方不维护 `{ORBITEXCH,SHARPEXCH}` 集合。
**验收**:helper 已落地并由 `tests/arbitrage/common/test_venues.py` 覆盖;strategy 的 mean_rebate / one_side_rebate / recovery / place_bets 已改为调用 helper。

## venues-6:risk helper

**输入**:PM/OE/SE 三类 instrument/order。
**期望**:概率门控、余额口径和 Portfolio outcome 公式经同一 registry odds_model 派生;余额/持仓/liveness 查询仍保留真实 venue identity。
**验收**:`tests/arbitrage/risk/test_engine.py` 的 PM/OE/SE 概率门控用例覆盖 helper 调用路径;`tests/arbitrage/risk/test_portfolio.py` 覆盖 outcome formula 与 OE/SE venue identity 分离;余额检查按 descriptor 口径判断 probability vs decimal venue。

## venues-7:settlement capability helper

**输入**:默认 PM+OE 配置、PM disabled + OE+SE 配置。
**期望**:`enabled_settlement_venues(cfg, "polymarket_ctf")` 只在声明该 capability 的 venue 已启用时返回 PM descriptor;PM disabled 时返回空,launcher 不再直接按 `POLYMARKET` 常量决定 settlement 是否初始化。
**验收**:已落地。`tests/arbitrage/common/test_venues.py` 覆盖 capability + enablement 派生,`tests/arbitrage/launchers/test_arb_node.py` 覆盖 PM disabled 时不构造 settlement。

## venues-8:skip_execution venue 组合 smoke matrix

**前置**:配置分别启用 PM+OE、PM+SE、OE+SE、PM+OE+SE;`debug.skip_execution=true`;`data_sources.sports_status.enabled=true`。
**输入**:经 `launchers/arb_node.py` 启动真实连接但跳过真实订单 IO。
**期望**:每组只注册 enabled tradable venues 与 PMSPORTS data source;Matching 走 `.PMSPORTS` anchor 聚合 tradable venues;Strategy/Risk/Execution 不消费 `.PMSPORTS` anchor;无真实下单。
**验收**:部分完成。PM+SE smoke 已由 SE README 记录;PM+OE、OE+SE-only、PM+OE+SE 仍待按用户明确要求启动后验收。

## venues-9:订单方向与持仓不变量

**输入**:PM BUY/SELL、decimal BUY/SELL，以及 probability venue SHORT Position。
**期望**:`order_exposure_probability` 对 SELL 返回获得的互补敞口概率；
`order_required_balance` 是 Strategy/Risk/Execution 的实际订单资金需求唯一入口；probability SHORT
抛 `PositionOutcomeInvariantError`，不静默映射或丢弃。
**验收**:`tests/arbitrage/common/test_venues.py` 覆盖 PM SELL 互补概率、各订单方向资金需求和
probability SHORT fail-closed；具体经济公式的唯一真理源为 `venues.md §4.1`。
