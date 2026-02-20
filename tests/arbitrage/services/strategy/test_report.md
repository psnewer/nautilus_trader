# Strategy 服务测试报告

## 执行信息

- 执行命令：`pytest tests/arbitrage/services/strategy/test_strategy_service.py -q`
- 执行结果：通过
- 用例数：14
- 通过数：14
- 失败数：0

## 用例结果

- 用例 1：单平台赔率更新触发信号计算（rebate） — 通过
- 用例 1 结果：rebate 信号写入缓存
- 用例 2：双平台赔率更新触发策略与机会生成 — 通过
- 用例 2 结果：default 策略触发，机会列表新增
- 用例 3：赛前/赛中信号联动 — 通过
- 用例 3 结果：pre-match=true/live=false；切换 is_live 后 pre-match=false/live=true
- 用例 4：multi-way 过滤方向 — 通过
- 用例 4 结果：remaining_count=3，removed_count=1（polymarket:away >= 0 被过滤）
- 用例 5：比赛级别 signal_overrides 生效 — 通过
- 用例 5 结果：rebate rate_threshold=0.1
- 用例 6：未注册比赛不触发计算 — 通过
- 用例 6 结果：signals 为空
- 用例 7：mean_rebate 信号计算 — 通过
- 用例 7 结果：mean_rebate value 非空
- 用例 8：策略选出最优方向 — 通过
- 用例 8 结果：best_direction=pair-8_rebate_away_orbit，legs=orbitexch away/home，total_probability=71.25，rebate_rate=0.92
- 用例 9：rebate 阈值生效（高/低阈值） — 通过
- 用例 9 结果：高阈值 satisfied=false；低阈值 satisfied=true
- 用例 10：3-way 比赛信号计算 — 通过
- 用例 10 结果：rebate details 无 market incomplete 错误
- 用例 11：策略筛选优先级（负返水优先） — 通过
- 用例 11 结果：best_direction rebate_market=home，rebate_venue=polymarket
- 用例 12：策略筛选优先级（最小正返水优先） — 通过
- 用例 12 结果：best_direction rebate_market=home
- 用例 13：策略筛选优先级（机会返水率优先） — 通过
- 用例 13 结果：best_direction 为机会返水率最大方向
- 用例 14：平台缺失价格仍保留方向 — 通过
- 用例 14 结果：directions 非空
