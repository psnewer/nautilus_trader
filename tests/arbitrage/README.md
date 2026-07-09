# 套利系统测试

本目录是套利系统(`src/arbitrage/` + `nautilus_trader/adapters/{polymarket,orbitexch}/`)的测试套件,**不**包含 NT 框架自身测试(后者在 `tests/unit_tests/` `tests/integration_tests/` 等)。

## 设计依据

所有测试用例对应 `docs/arbitrage/refactor.md` 中的设计决定。修改测试前先阅读该文档。

## 目录组织

按 P8 capability 分组,镜像 `src/arbitrage/` / `nautilus_trader/adapters/` 树形:

| 目录 | 对应源码 / 设计章节 | 状态 |
|---|---|---|
| `config/` | `src/arbitrage/config/` / `_cross-cutting/configuration.md` | 详细(ArbConfig schema/dispatcher/loader) |
| `venues/` | `src/arbitrage/common/venues.py` / `_cross-cutting/venues.md` | 详细(Venue Registry/capability) |
| `discovery/` | `src/arbitrage/discovery/` + `nautilus_trader/adapters/{venue}/providers.py` / `refactor.md §5.1, §5.2.2` | **详细** (Step 1-2) |
| `matching/` | `src/arbitrage/matching/` / `refactor.md §5.3, §6.4` | 详细(Step 3) |
| `adapters/polymarket/` | 上游 `nautilus_trader/adapters/polymarket/*` / `refactor.md §6.5` | 详细(验证上游适配器满足需求) |
| `adapters/orbitexch/` | `nautilus_trader/adapters/orbitexch/*` / `refactor.md §6.2` | 详细(自写适配器) |
| `adapters/sharpexch/` | `nautilus_trader/adapters/sharpexch/*` / `architectures/sharpexch/architecture.md` | 详细(SE adapter) |
| `strategy/` | `src/arbitrage/strategy/` / `refactor.md §5.4` | 详细(Step 4) |
| `risk/` | `src/arbitrage/risk/` / `refactor.md §5.6` | 详细(Step 6 离线门控;节点级/live 仍按子 README 标注) |
| `execution/` | execution 横切 helpers / adapter factory 边界 | 摘要 |
| `web/` | `src/arbitrage/web/` / `refactor.md §5.7` | 详细(Step 7 控制台) |
| `debug/` | `src/arbitrage/debug/` / `refactor.md §6.6` | 详细(Q11 Debug 注入) |
| `launchers/` | `launchers/arb_node.py` | 详细(Node wiring) |
| `settlement/` | PM settlement 编排 / contract IO | 部分离线覆盖,链上/live 待验 |
| `e2e/` | 端到端套利场景 | 部分离线覆盖,真钱 E2E 待验 |
| `_helpers/` | 公共 fixture / 配置文件 / mock 数据 | 持续维护 |

## 形态

每个 capability 目录:
- `README.md` —— 用例清单(每个用例: 前置 / 输入 / 步骤 / 期望 / 验收标准)
- `test_*.py` —— 当前主路径多数已是可执行 pytest;少量需要全节点、浏览器或真钱的场景在对应 README 中显式标为 live/e2e 待验。

## 命名约定

测试 ID 格式: `<capability_prefix>-<step>.<n>`,例如 `discovery-1.3` = discovery 模块 Step 1 的第 3 个用例。

## 跑测

```bash
# 仅跑套利系统测试(不含 NT 框架)
pytest tests/arbitrage/

# 跑某个 capability
pytest tests/arbitrage/discovery/

# 跑某个用例
pytest tests/arbitrage/discovery/test_orbitexch_provider.py::test_oe_provider_cold_start
```

## conftest.py

`tests/arbitrage/conftest.py` 覆盖了 NT 根 conftest 中已弃用的 `event_loop` fixture。**保留此文件**,所有 arbitrage 测试都依赖它工作。
