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
- `audit_nohup_orders.py`：以 `OrderInitialized` 为下单时间锚点，从 `nohup.out` 重建全部订单、下单比分、start price、venue 变化和未成交单最接近限价的 OBD；按 prepare 前最新 live Cache 帧（包括因 `pair_in_flight` 跳过 evaluate、但仍已更新 Cache 的行情帧）结合候选 rate 与过滤日志反推 `venue_replace` 前的原生 venue/方向。`start_game` 的 prepare 日志中 `rate=None`，其单腿不经过候选套利腿与 `venue_replace`，因此直接以最终 PM 腿作为规则输入腿。
- `write_order_audit_sheet.py`：把上述 JSON 写入既有工作簿的“下单汇总/下单明细”页，并移除曾按成交时间错误追加的五行。
- `write_uniform_rule_sheet.py`：把单份 `nohup.out` 中全部已成交/部分成交订单写入“nohup成交_统一规则”页；不按局分过滤，以 `venue_replace` 前方向对应的 PM bid 对比 start price，区分 `start_game`、`convert`、`attitude`、`deviate_convert`、tier pre/post 与 tier ignore；同一比赛重复出现的“未成交-风控拒绝”只保留最早一笔，其他订单状态不去重。历史日志启用 `convert=true` 时须传 `--venue-replace-convert`。缺原始方向时留空并标注无法确认，不再用最终方向兜底。追加模式同时兼容旧 34 列报表和带 pre/post、官方赛果的 38 列总盘。推断赛果时只统计已完成盘，不把日志末尾仍在进行的当前盘计作盘胜负。
- `recalculate_reverse_profit.py`：针对既有低级别赛事样本，分别重算“原始腿取反”和“实际下单方向取反”；价格优先使用下单时目标方向的直接 PM bid，仅在该 bid 缺失时使用 `1 - 对向 ask`，不应用 spread。
- `recalculate_current_rule_directions.py`：按当前 `venue_replace` 优先级重放历史订单方向；原生 venue 优先取审计 JSON，低级别赛事优先取日志中的 OE competition。旧日志缺失时会显式标记从历史 action / position 元数据回退，避免把推断当成事实。
- `rebuild_strategy_profit_workbook.py`：使用事先缓存的 Polymarket 官方 market 元数据重建全量订单的低级别分类与官方结算胜方，并同步重算 tier pre/post 方向、无 spread 利润、spread=0.05 利润及分类汇总。胜方以 market token `winner` 为准，并校验 token outcome 与 pair 选手顺序；官方尚未结算的订单写入方向明细，但暂不纳入利润。总盘排除风控拒绝单，同场只计一笔并按“已成交 > 部分成交 > 其它状态”优先保留。该脚本不主动联网，并在工作簿中写入分类来源与重算口径。

这些脚本源自一次性审计，仍保留当次报告日期、桌面文件名和 `/private/tmp` 输入路径。再次运行前必须先检查文件顶部的路径常量；脚本不包含凭证，也不会主动连接远端或下单。
