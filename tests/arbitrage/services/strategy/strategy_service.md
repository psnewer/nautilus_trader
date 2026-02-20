# Strategy 服务测试用例

说明：测试数据使用 `fixtures/strategy_cases.json`，按 case_id 对应。

用例 1：单平台赔率更新触发信号计算（rebate）  
数据：case_1  
前置条件：StrategyService 启用；signals 含 rebate；已注册比赛  
步骤：仅触发 Polymarket 赔率更新；查询信号缓存  
预期：rebate 信号结果写入缓存

用例 2：双平台赔率更新触发策略与机会生成  
数据：case_2  
前置条件：StrategyService 启用；默认策略包含 rebate  
步骤：依次触发 Polymarket 与 OrbitExch 赔率更新；查询策略结果与机会列表  
预期：strategy 触发为 true；机会列表新增

用例 3：赛前/赛中信号联动  
数据：case_3  
前置条件：策略包含 pre-match 与 live；已注册比赛  
步骤：先设置 is_live=false 触发赔率更新；再设置 is_live=true 触发赔率更新  
预期：赛前信号与赛中信号分别按状态满足/不满足

用例 4：multi-way 过滤方向  
数据：case_4  
前置条件：signals 含 rebate + multi-way；风险服务返回负向 way_rebate  
步骤：触发双平台赔率更新；检查 multi-way 过滤结果  
预期：含负向 outcome 的方向被移除；信号仍返回剩余方向数量

用例 5：比赛级别 signal_overrides 生效  
数据：case_5  
前置条件：配置 match_config 覆盖 rebate 参数；已注册比赛  
步骤：触发双平台赔率更新；检查 rebate 详情中的 rate_threshold  
预期：使用覆盖后的 rate 参数

用例 6：未注册比赛不触发计算  
数据：case_6  
前置条件：未注册比赛；signals 含 rebate  
步骤：触发赔率更新；查询信号缓存  
预期：无信号结果产生

用例 7：mean_rebate 信号计算  
数据：case_7  
前置条件：signals 含 mean_rebate；已注册比赛  
步骤：触发双平台赔率更新；查询信号缓存  
预期：mean_rebate 信号有结果；value 非空

用例 8：策略选出最优方向  
数据：case_2  
前置条件：signals 含 rebate；已注册比赛  
步骤：触发双平台赔率更新；查询 best_direction  
预期：best_direction 非空

用例 9：rebate 阈值生效（高/低阈值）  
数据：case_9  
前置条件：signals 含 rebate；比赛级别覆盖 rate  
步骤：先使用高阈值触发更新；再使用低阈值触发更新  
预期：高阈值 satisfied=false；低阈值 satisfied=true

用例 10：3-way 比赛信号计算  
数据：case_10  
前置条件：signals 含 rebate；已注册比赛；含 draw 赔率  
步骤：触发三方向赔率更新；查询信号缓存  
预期：rebate 信号无 market incomplete 错误

用例 11：策略筛选优先级（负返水优先）  
数据：case_11  
前置条件：signals 含 rebate；风险服务提供 way_rebate（home 为负）  
步骤：触发双平台赔率更新；查询 best_direction  
预期：best_direction 的 rebate_market=home

用例 12：策略筛选优先级（最小正返水优先）  
数据：case_12  
前置条件：signals 含 rebate；风险服务提供 way_rebate（均为正）  
步骤：触发双平台赔率更新；查询 best_direction  
预期：best_direction 的 rebate_market=home（home 为最小正返水）

用例 13：策略筛选优先级（无持仓数据按 rebate_rate）  
数据：case_13  
前置条件：signals 含 rebate；无负返水且合并 way_rebate 为空  
步骤：触发双平台赔率更新；查询 best_direction  
预期：best_direction 为机会返水率最大方向

用例 14：平台缺失价格仍保留方向  
数据：case_14  
前置条件：signals 含 rebate；非返水方向一侧平台缺失价格  
步骤：触发赔率更新；查询 directions  
预期：仍生成套利方向
