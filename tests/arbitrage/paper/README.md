# Paper execution 测试

详细设计：`docs/arbitrage/architectures/paper/architecture.md`。

## paper-1：本地账本与回执 schema

**前置**：新 SQLite 账本，固定 best bid/ask。
**输入**：可立即成交的 BUY limit。
**期望**：订单转 MATCHED，现金/仓位/成本在本地更新；PLACEMENT 和
CONFIRMED trade 能被现有 USER WS schema 解码，持久化 trade 能被 REST report
schema 解码。
**验收**：`test_marketable_order_persists_fill_position_balance_and_decodable_ws`。

## paper-2：驻留单、FAK 与预留

**输入**：未交叉 GTC，后续交叉；未交叉 FAK；超过现金/仓位的第二笔单。
**期望**：GTC 仅在后续轮询交叉后以 maker 成交；FAK 自动撤单；未成交名义额与
SELL 数量均被预留，不得超用。
**验收**：`test_resting_gtc_fills_only_after_later_price_cross` /
`test_unmatched_ioc_is_canceled` / `test_open_orders_reserve_balance_and_position`。

## paper-3：重试幂等

**输入**：完全相同 signed payload 提交两次。
**期望**：返回同一本地 order ID，SQLite 只有一条订单。
**验收**：`test_duplicate_submission_is_idempotent`。

## paper-3b：并发预占原子性

**输入**：两笔并发 BUY，单笔可由余额覆盖，两笔合计超过余额。
**期望**：市场查询后的账本临界区重新检查可用额，仅一笔订单进入 LIVE；不得因异步查询
窗口让两笔同时预占成功。
**验收**：`test_concurrent_orders_cannot_overreserve_balance`。

## paper-4：HTTP/WS 协议回路与待补进程级集成

**前置**：启动 localhost paper server，禁止任何真实私有 PM 写路由。
**步骤**：用真实 `py_clob_client_v2` 向本地端点发 submit/cancel/report，用 adapter
连 USER WS，重启 arb node 后观察 reconcile。
**期望**：订单生命周期进 Cache/Portfolio，重启后从 SQLite 恢复，无真实私有
PM/OE/SE 写请求。
**离线验收**：`test_http_and_user_ws_protocol_round_trip` 已覆盖本地 HTTP submit/report/
positions 与 USER WS placement/trade 回路；`test_paper_server_launcher_passes_public_pm_proxy`
钉住 launcher 的 proxy 传递，`test_public_market_query_uses_configured_proxy` 钉住 matcher
公开 PM 请求实际使用该 proxy；`test_real_clob_sdk_submit_and_reports_round_trip` 使用真实
`py_clob_client_v2.ClobClient` 验证 submit → get_order/get_trades 以及驻留单
get_open_orders → cancel → get_order 协议。
**状态**：独立 server + arb node 进程级运行时验收仍待补；不用离线绿色
替代该路径证据。
