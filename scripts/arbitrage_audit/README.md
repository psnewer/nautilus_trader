# 套利成交审计脚本

这里保存从临时目录迁回的成交审计与 Excel 报表脚本，避免 `/private/tmp` 清理后丢失。

## 文件

- `analyze_arb_fills.py`：从 `nohup.out` / `log.out` 重建成交比分、start price、原生 venue、触发 venue 与 stale window，并包含历史利润/挂单成交性分析入口。
- `convert_trade_report_to_xlsx.py`：把管道分隔的成交报告转换为 Excel。
- `calculate_native_pm_profit.py`：生成原生 PM 腿利润测算页。
- `add_opposite_pm_profit.py`：生成对手 PM 腿利润测算页。
- `scan_final_scores.py`：补充最终比分。
- `append_five_trade_rows_20260924.py`：2026-09-24 五笔成交追加操作的可复现快照。
- `append_harold_mayot_20260926.py`：把 Harold Mayot 54c 订单及“比分门控后 convert 反转”的诊断结论追加到比分报表。
- `audit_nohup_orders.py`：以 `OrderInitialized` 为下单时间锚点，从 `nohup.out` 重建全部订单、下单比分、start price、venue 变化和未成交单最接近限价的 OBD；能从候选腿与过滤日志唯一反推出时，额外记录 `venue_replace` 前的规则输入方向。
- `write_order_audit_sheet.py`：把上述 JSON 写入既有工作簿的“下单汇总/下单明细”页，并移除曾按成交时间错误追加的五行。
- `write_uniform_rule_sheet.py`：把单份 `nohup.out` 中全部已成交/部分成交订单写入“nohup成交_统一规则”页；不按局分过滤，以 `venue_replace` 前方向对应的 PM bid 对比 start price，按现行 `bid<=start` 或 `bid>=1.2×start`（无上限）规则测算，缺值时明确标注且不做默认兜底，避免拿实际成交方向重复应用反买规则；推断赛果时只统计已完成盘，不把日志末尾仍在进行的当前盘计作盘胜负。

这些脚本源自一次性审计，仍保留当次报告日期、桌面文件名和 `/private/tmp` 输入路径。再次运行前必须先检查文件顶部的路径常量；脚本不包含凭证，也不会主动连接远端或下单。
