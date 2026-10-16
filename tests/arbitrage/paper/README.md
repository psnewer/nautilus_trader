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
**期望**：GTC 由后续 Market WS 帧交叉后立即以 maker 成交，成交价是订单 limit 而非
触发帧 quote；触价后一帧立刻回撤也不漏成交。FAK 自动撤单；未成交名义额与 SELL 数量
均被预留，不得超用；`bid >= ask` 的异常帧不发布也不撮合。
**验收**：`test_resting_gtc_fills_only_after_later_price_cross` /
`test_crossed_market_frame_is_not_published_or_matched` /
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
钉住 launcher 把真实 Market WS URL 与 proxy 传给 paper 服务，
`test_public_market_query_uses_configured_proxy` 钉住公开 PM 请求实际使用该 proxy；
`test_real_clob_sdk_submit_and_reports_round_trip` 使用真实
`py_clob_client_v2.ClobClient` 验证 submit → get_order/get_trades 以及驻留单
get_open_orders → cancel → get_order 协议。
**状态**：独立 server + arb node 进程级运行时验收仍待补；不用离线绿色
替代该路径证据。

## paper-5：订单 metadata、旧库迁移与 Excel 归集

**前置**：已有旧 schema SQLite，或新 paper 订单已由 ExecClient 取得 venue order ID。
**输入**：`pair_id/origin/intent`、下单比分等 NT Order metadata；一笔含成交的 paper 订单；
以及按 `client_order_id` 关联的撤单日志审计 JSON。
**期望**：旧库自动增加可空审计列；metadata 端点按 venue order ID 更新且不改变订单终态；
Excel 包含“订单明细/Pair汇总/类型汇总/说明”，并以 `origin/intent` 标识订单类型；撤单行带
撤单时间、撤单前最近比分及 PM/OE/SE bid/ask；成交行带首次成交前最近比分和盘号，日志
缺失值保持为空。
**验收**：`test_existing_database_is_migrated_and_order_metadata_is_persisted` /
`test_http_and_user_ws_protocol_round_trip` / `test_export_workbook_writes_detail_pair_and_type_sheets` /
`test_export_workbook_merges_cancel_score_and_quotes_from_log_audit`。

## paper-6：按运行重置账本

**前置**：已有包含历史订单、余额及 WAL/SHM sidecar 的 paper SQLite。
**输入**：以 `reset=true` 创建 `PaperStore`。
**期望**：建立连接前清除旧主库与 sidecar；新账本无历史订单，余额等于本次
`starting_balance`。`reset=false` 仍保留原持久化语义。
**验收**：`test_reset_recreates_empty_ledger_with_starting_balance`；launcher 测试钉住配置透传。
