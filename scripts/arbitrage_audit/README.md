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
- `append_remote_order_detail.py`：把订单审计 JSON 写入独立真实下单明细 sheet，并从下单前的 `ConsecutiveTriggerGate` 历史还原同方向连续触发的前后两个比分；非连续触发订单留空。日志轮转后可用 `--append` 保留旧行，并按 `client_order_id` 去重追加新日志。
- `write_order_audit_sheet.py`：把上述 JSON 写入既有工作簿的“下单汇总/下单明细”页，并移除曾按成交时间错误追加的五行。
- `write_uniform_rule_sheet.py`：把单份 `nohup.out` 中全部已成交/部分成交订单写入“nohup成交_统一规则”页；不按局分过滤，以 `venue_replace` 前方向对应的 PM bid 对比 start price，区分 `start_game`、`convert`、`attitude`、`deviate_convert`、tier pre/post 与 tier ignore；同一比赛重复出现的“未成交-风控拒绝”只保留最早一笔，其他订单状态不去重。历史日志启用 `convert=true` 时须传 `--venue-replace-convert`。缺原始方向时留空并标注无法确认，不再用最终方向兜底。追加模式同时兼容旧 34 列报表和带 pre/post、官方赛果的 38 列总盘。推断赛果时只统计已完成盘，不把日志末尾仍在进行的当前盘计作盘胜负。
- `recalculate_reverse_profit.py`：针对既有低级别赛事样本，分别重算“原始腿取反”和“实际下单方向取反”；价格优先使用下单时目标方向的直接 PM bid，仅在该 bid 缺失时使用 `1 - 对向 ask`，不应用 spread。
- `recalculate_current_rule_directions.py`：按当前 `venue_replace` 优先级重放历史订单方向；原生 venue 优先取审计 JSON，低级别赛事优先取日志中的 OE competition。旧日志缺失时会显式标记从历史 action / position 元数据回退，避免把推断当成事实。
- `rebuild_strategy_profit_workbook.py`：使用事先缓存的 Polymarket 官方 market 元数据重建全量订单的低级别分类与官方结算胜方，并同步重算 tier pre/post 方向、无 spread 利润、spread=0.05 利润及分类汇总。胜方以 market token `winner` 为准，并校验 token outcome 与 pair 选手顺序；官方尚未结算的订单写入方向明细，但暂不纳入利润。总盘排除风控拒绝单，同场只计一笔并按“已成交 > 部分成交 > 其它状态”优先保留。该脚本不主动联网，并在工作簿中写入分类来源与重算口径。
- `unify_simulation_log.py`：解析 `PlaceBets[simulation]`，关联同一 pair 最近的比分、start price、PM/OE OBD 与连续触发原腿，输出原始触发、逐场归并和口径说明三个 sheet。
- `add_simulation_settlement.py`：在 simulation 报表中按“每场首条模拟单”补充胜负、毛利润、手续费与净利润；重复 simulation 触发不重复计单。
- `apply_sim_price_cap.py`：在 simulation 首单结算基础上应用价格上限，分别生成纳入结算和被价格风控排除的明细 sheet。
- `replay_simulation_venue_replace.py`：以 simulation 原始触发为输入，按当前 `VenueReplaceAction` 优先级回放 `convert → attitude → deviate_convert`（不启用 tier 转换），在最终 PM ask 上应用价格上限，并按 pair 选择首个真正合格机会计算胜负和利润。
- `replay_all_venue_replace.py`：合并历史 `draw|win` 标准化信号与后续 simulation 新增场次，统一回放 `convert/attitude/deviate_convert`，在反转后的 PM ask 上应用价格上限，输出全量逐场明细及按动作拆分的胜负、毛利、手续费和净利润；同时保留 `deviate_convert` 无上限及 `1.2≤bid/start≤1.3` 两种口径，并对后者按低级别、非低级别、等级未分类及动作交叉汇总。
- `compare_fixed_replay_reversals.py`：固定既有 `draw_win真实回放` 的每场首个有效信号，不重新选择连续触发机会；从同时间日志帧补齐原腿 venue、双方 PM 报价与最近 start price，在完全相同的256条信号上配对比较“不反转”和 `convert/attitude/deviate_convert(1.2–1.3)`，并分别应用最终 PM ask 上限与手续费。

这些脚本源自一次性审计，仍保留当次报告日期、桌面文件名和 `/private/tmp` 输入路径。再次运行前必须先检查文件顶部的路径常量；脚本不包含凭证，也不会主动连接远端或下单。新写的可复用审计脚本应在本回合结束前保存到本目录，`/private/tmp` 仅存日志、缓存和中间产物。
