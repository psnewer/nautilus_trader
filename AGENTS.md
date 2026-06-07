# AGENTS.md — Codex 开发准则(NautilusTrader 跨市场套利系统)

> 2026-06-07:本文件由 Claude Code 的配置/知识迁移而来。**Codex 现为本项目主开发者**(规划 + 执行 + 回写文档 + 测试 + 最终落地)。旧版把 Codex 定位为"审查者"的内容已废弃。

## 项目概览

基于 **NautilusTrader(NT)** 构建的跨市场体育赛事套利系统:**Polymarket**(gamma 发现 + CLOB 下单)↔ **OrbitExch**(Playwright 抓取的博彩交易所)。真实个人账户、真钱交易。

当前在 `refactor/NT` 分支,正从老的 `src/arbitrage/services/`(web_gateway 微服务栈)迁移到 **NT 原生**架构(`nautilus_trader/adapters/` 自写适配器 + `launchers/arb_node.py` 启动)。

## ⭐ 先读这个

**`docs/arbitrage/refactor.md`** —— 决策日志 + 决策史(Q1–Q20)+ 修订记录(#1–#65)。**任何设计/编码工作前先读它了解"为什么"**,它是"理由/历史"的单一真理源。

## 项目文档索引

| 文档 | 路径 | 说明 |
|------|------|------|
| 迁移决策日志 | `docs/arbitrage/refactor.md` | **先读**:决策史 + 修订记录(为什么) |
| 架构总览 | `docs/arbitrage/architecture.md` | NT 端态架构总览 + 组件导航 |
| 组件详细设计 | `docs/arbitrage/architectures/<组件>/architecture.md` | 接口/数据流/算法/时序(设计真理源) |
| 需求说明 | `docs/arbitrage/requirements/` | 行为真理源(按旧服务名组织,语义仍有效) |
| 数据库设计 | `docs/arbitrage/database-schema.md` | PostgreSQL / Redis |
| NautilusTrader | `docs/arbitrage/NautilusTrader.md` | 框架说明、组件职责、适配器开发 |
| 测试架构 | `docs/arbitrage/debug-framework.md` | 实盘/模拟盘测试架构 |
| **累积知识** | `docs/arbitrage/agent-notes/INDEX.md` | **从 Claude 记忆迁移**:bug 状态 / Gap C 进展 / 工程陷阱 / 协作经验 |

## 设计文档分层(单一真理源)

| 层 | 落位 |
|---|---|
| 决策日志(为什么 + 定了什么 + 指针) | `docs/arbitrage/refactor.md`(仅"理由/历史") |
| 详细设计(是什么/怎么做) | `architecture.md` 总览 + `architectures/<组件>/architecture.md` |
| 需求(行为真理源) | `docs/arbitrage/requirements/...` |
| 测试用例 | `tests/arbitrage/<capability>/README.md` |

做非琐碎设计/架构前**先调 `design-docs` skill**(分层 + 流程 + 规则 + 反模式)。冲突时:详细设计有把握 → 以详细设计为准并回写 refactor.md 修订记录;没把握 → 和用户讨论。

## Skills(已迁移到 `~/.codex/skills/`)

| Skill | 何时用 |
|---|---|
| `design-docs` | 非琐碎的架构/详细设计、组织/重构设计文档、拿不准某机制归哪份文档时 |
| `live-test` | "实盘测试 / live test / 跑一遍 \<scenario\>" —— 在本套利系统上跑人在回路实盘场景测试 |
| `live-test-base` | live-test 的通用元流程骨架(被 `live-test` 继承,一般不直接调) |
| `utils-manager` | 需要通用工具函数(time/numeric/string)时,优先复用现有 |

## 编码准则(karpathy)

遵循 karpathy 准则降低 LLM 编码常见错误:**最小/外科手术式改动、不过度工程、主动暴露假设、定义可验证的成败标准**。写/改/审代码时套用。

## 📌 文档同步纪律(原 Claude PostToolUse hook → 现为硬规则)

**改了代码,本回合收尾前必须同步对应文档**(否则文档与代码脱节)。映射:

- 改 `src/arbitrage/<cap>/...` → 同步 `docs/arbitrage/architectures/<cap>/architecture.md`
- 改 `nautilus_trader/adapters/{polymarket,orbitexch}/...` → 同步 `docs/arbitrage/architectures/{execution,data,discovery}/architecture.md`(按实际涉及的)
- 改了 `docs/arbitrage/**/architecture.md` 且涉及**组件功能/边界/数据流变化** → 同步对应 capability 的测试 README(下表)
- 改 `__init__.py` / `pyproject.toml` / `setup.py` → 检查本文件(项目概览/文档索引)是否需更新

capability → 测试 README:

| 设计变化涉及 | 同步目标 |
|---|---|
| Step 1-2(Provider / InstrumentRefresher / DataClient) | `tests/arbitrage/discovery/README.md` |
| Step 3(MatchingActor / 异构归一) | `tests/arbitrage/matching/README.md` |
| 上游 PM 适配器 | `tests/arbitrage/adapters/polymarket/README.md` |
| OE 自写适配器(Browser / Provider / DataClient / ExecutionClient) | `tests/arbitrage/adapters/orbitexch/README.md` |
| Step 4 ArbitrageStrategy | `tests/arbitrage/strategy/README.md` |
| Step 6 Risk(ArbitrageRiskEngine / ExecutionClient 账户状态) | `tests/arbitrage/risk/README.md` |
| Step 7 WebGatewayActor | `tests/arbitrage/web/README.md` |
| Q11 Debug 注入框架 | `tests/arbitrage/debug/README.md` |
| 端到端套利场景 | `tests/arbitrage/e2e/README.md` |

**约定**:测试用例可暂不落 .py 代码,但 README 必须有记录(编号/前置/输入/步骤/期望/验收)。纯重构且 API 未变 → 显式说明并跳过。
**架构归类(P11)**:跨组件机制按"有无单一自然归属"判——有(一方是契约定义者)→ 放主方小节 + 交叉引用;无(≥3 对等方共维同一不变量)→ 单独成横切章节(§6.x)。判据见 refactor.md §2。

## 代码准则

- 注释/文档一律用**简体中文**。
- **禁止无证据假设**;主动删除过时/重复/逃生式(escape-hatch)代码。
- 输出路径必须在架构/需求中有依据,**禁止自造路径**。
- **优先复用 NT 官方 API + 生态成熟方案**,除非确无法满足才自研。
- 事件驱动:组件经 MessageBus 通信;自定义组件继承 `Actor`/`Strategy`;外部市场接入实现 `DataClient` + `ExecutionClient`。

## 当前迁移状态(交接重点)

- 分支 `refactor/NT`,主分支 `develop`。
- **Gap C(OE 执行端 NT 适配器,`nautilus_trader/adapters/orbitexch/execution.py`)**:下单 / 撤单 / 成交回执(`_on_current_bets`→`generate_order_filled`)/ order reconcile(`generate_order_status_report(s)`)**代码层全通 + 离线单测**(`tests/arbitrage/adapters/orbitexch/test_execution_translation.py`,20 case);**但尚未 live 验证**。
  - ⚠️ **`place_and_cancel` scenario 跑的是老 `services/` 栈,不验 NT 适配器**;Gap C 的 live 验证必须走 `launchers/arb_node.py`。细节见 `docs/arbitrage/agent-notes/`。
- 仍待:matched 成交帧填充值(需真成交)、补偿撤单触发逻辑、PM 适配器 NT-node live 验。

## 🔴 安全红线(真钱!)

- 实盘连**真实个人账户**(Polymarket + OrbitExch),live test 会下**真单**。`skip_execution=true` 避免真单。
- 真单(`skip_execution=false`)= 不可逆真钱操作,**必须先取得用户明确确认**才跑。
- **不要在无用户触发的情况下自行启动 launcher / 跑实盘。**
- 凭证只走环境变量 / `.env`,**绝不写进受版控文件、绝不打印密钥值**。
- 不擅自修改 `debug_config.json` / `arb_config.example.json` 而不通报用户。
- 单腿失败会在 venue 留下活跃挂单(补偿撤单未接,见 agent-notes)→ 实盘出现"一腿成一腿失败"立即停下报告。

## MCP / 工具(已迁移到 `~/.codex/config.toml`)

11 个:`sequential-thinking`(深入分析)、`shrimp-task-manager`(任务规划)、`code-index`(代码检索)、`exa` / `fetch`(联网)、`context7`(库文档)、`chrome-devtools` / `playwright` / `puppeteer` / `browser-tools`(浏览器)、`filesystem`。
