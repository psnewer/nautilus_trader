# 风控服务（Risk）消息架构

## 消息发布

风控服务通过 NautilusTrader MessageBus 发布以下消息：

1. 持仓返水率消息
   - 主题：`arbitrage.way_rebate.{pair_id}`
   - 数据：`WayRebateMessage`
   - 发布位置：`src/arbitrage/services/risk/service.py::_publish_way_rebate`
   - 触发条件：
     - 会话完成后 `_refresh_all_way_rebates()` 刷新持仓
     - `load_historical_positions()` 加载历史持仓
     - 健康检查循环中 `_run_health_check()` 刷新 way_rebate

2. Pair 活跃互斥（pair_id 锁定）
   - 主题：`arbitrage.pair_activity.{pair_id}`
   - 数据：`PairActivityMessage`
   - 发布位置：`src/arbitrage/services/risk/service.py::_publish_pair_activity`
   - 触发条件：
     - 收到 `session_complete` 后进入 post-session 刷新流程时发送 `is_active=true`
     - 解锁依赖 OddsSubscription 的超时清理（Risk 不主动发送 `is_active=false`）

## 消息订阅

1. 会话完成（来自 ExecutionService）
   - 订阅主题：`arbitrage.session_complete.*`
   - 处理：查询 odds_service 获取持仓数据 → 刷新持仓 → 发布 way_rebate
   - 订阅位置：`src/arbitrage/services/risk/service.py::set_msgbus`

## 同步依赖（DI）

- `odds_service` — 用于查询 Polymarket/OrbitExch 持仓数据和映射
- `execution_service` — 用于健康检查访问 Polymarket client（`get_ok()`）和 OrbitExch pages（CSRF token）
- `check_risk()` — 被 StrategyService 同步调用，作为机会发布前的门控

## 健康检查

RiskService 承担全局健康检查职责，并**驱动两个适配器的"首次开页 / 周期性恢复"统一路径**——
``subscribe_event`` / ``subscribe_competition`` 只 wire WebSocket / 记账，所有 IO（OE 开页、PM REST 拉取）都由健康检查触发。

**每轮检测三项指标**（`_run_health_check`）：

1. **Polymarket（每轮无条件主动 fetch）**：`get_ok()` + `get_balance_allowance()` + `fetch_positions()` + `fetch_open_orders()`。
   PM Data API 偶发不可用，每轮重拉以及时反映 venue 状态。任一步骤失败 → `pm_healthy=False`。
2. **OrbitExch（按当前健康状态分支）**：
   - 当前 `oe_healthy=False` → 调用 `oe_client.refresh_page()`，由 adapter 内部 `_open_or_reload_page` 统一首次 `goto`（page 不存在）
     和 `reload`（page 已存在）。等待 `CURRENT_BETS` 推送即视为就绪。
   - 当前 `oe_healthy=True` → 用 `page.evaluate('/customer/api/currentBets')` 做轻量探测，确认 session 仍有效。
   - 任一分支失败 → 尝试一次 "重新登录 + refresh" 兜底。
   - 新打开的 competition page 会通过 `_sync_oe_executor_pages` 同步给 `OrbitExchExecutor` 用于下单。
3. **Way Rebate**：从已 fetch 的数据计算 `_refresh_all_way_rebates`（不触发 IO）。

三项全部通过时 `_health_ok = True`，否则 `check_risk()` 在 `execution_enabled` 检查之后、风控逻辑之前直接拒绝机会。

**启动流程**：
1. `subscribe_matched_pairs` → 仅 wire WS / 注册 competition（无 IO）
2. `risk_service.run_initial_health_check()` → 同步跑一次：触发 OE 首次开页、PM 主动 fetch
3. `load_risk_historical_positions` → 此时缓存已就绪，构建 risk position manager
4. `risk_service.start_health_check_loop()` → 进入 120s 一次的常规节奏

配置：`RiskConfig.health_check_interval_sec`（默认 120s，可在 web 面板调整）。

接线位置：`AppState.ensure_execution_registered()` 注入依赖 + `web_gateway/routes/odds.py` 在订阅完成后驱动启动序列。

### 触发逻辑（block / unblock / active trigger）

健康检查循环改为 **schedule + event-driven**，不再用 sleep-after-run：

- 状态：`_next_health_check_at`（下次唤醒的 monotonic 时间）+ `_health_check_event`（主动触发事件）+ `_health_check_blocked`（阻塞标志）。
- 每轮节奏：
  1. `await asyncio.wait_for(_health_check_event.wait(), timeout=直到 next_at)` —— 时间到或主动触发都会唤醒。
  2. 唤醒后**先把 `_next_health_check_at` 置空**（之后被 active trigger 唤醒时不会受旧时间干扰）。
  3. 检查 `_health_check_blocked`：阻塞 → 跳过执行体（**不改变 pm/oe/rebate 任何健康标志**）；放通 → 跑 `_run_health_check`。
  4. 每轮结束前用当前 `health_check_interval_sec` 重新规划 `_next_health_check_at = monotonic + interval`（支持配置动态更新）。

外部信号 API：

| 方法 | 用途 |
|------|------|
| `block_health_check()` | 设置阻塞标志，下一轮唤醒时跳过执行体（结果保留上次） |
| `unblock_health_check()` | 清除阻塞标志（不改变循环节奏） |
| `is_health_check_blocked()` | 查询当前是否阻塞 |
| `trigger_health_check()` | 设置 event，立即唤醒循环（仍受 block 拦截） |
| `get_next_health_check_at()` | 查询下次执行的 monotonic 时间（None = 正在执行 / 未规划） |

`get_health_status()` 同步返回 `blocked` 和 `seconds_until_next_check` 两个字段，供 web 面板展示循环节奏。

## 说明

- RiskService 不再被 ExecutionService 直接调用 `refresh_pair_position()`，改为订阅 `session_complete` 消息后自行刷新。
- way_rebate 值在 session 完成和健康检查循环中刷新，由 RiskService 主动推送到 MessageBus。
- StrategyService 缓存 way_rebate，不再每次赔率更新时主动读取。
- 健康检查从 ExecutionService 迁移到 RiskService，利用已有的 `check_risk()` 门控链路在上游拒绝机会，不在 ExecutionService 中阻塞等待。
