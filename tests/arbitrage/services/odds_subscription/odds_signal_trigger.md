# 赔率触发信号测试用例

用例 1：单平台赔率更新触发信号计算  
前置条件：Signal 已注册赔率消息；StrategyService 启用；signals 含 rebate；已注册比赛信息  
步骤：触发一次 Polymarket 赔率更新（market_type=home）；查询信号缓存  
预期：触发信号计算；pair_id 的 signal 结果写入缓存

用例 2：双平台赔率更新触发信号计算  
前置条件：Signal 已注册赔率消息；StrategyService 启用；signals 含 rebate；已注册比赛信息  
步骤：触发 Polymarket 赔率更新（home/away）；触发 OrbitExch 赔率更新（home/away）；查询信号缓存  
预期：触发信号计算；rebate 信号有结果；写入缓存

用例 3：重复订阅赔率消息不应重复触发  
前置条件：Signal 重复订阅赔率消息；回调去重已生效  
步骤：触发一次 Polymarket 与 OrbitExch 赔率更新；统计信号回调触发次数  
预期：回调仅一次；信号结果仅更新一次

用例 4：未注册比赛的赔率更新不触发信号  
前置条件：未注册该 pair_id；Signal 已注册赔率消息  
步骤：触发该 pair_id 的 Polymarket 与 OrbitExch 赔率更新；查询信号缓存  
预期：不产生 signal 结果；不生成机会数据

用例 5：未配置的赔率信号不应触发  
前置条件：策略仅配置 mean_rebate；已注册比赛信息  
步骤：触发 Polymarket 与 OrbitExch 赔率更新；查询信号缓存  
预期：仅 mean_rebate 有结果；rebate 不应计算

用例 6：信号触发顺序与缓存复用  
前置条件：策略配置 live + rebate；已注册比赛信息  
步骤：先触发比赛状态更新；再触发 Polymarket 赔率更新；查询信号缓存  
预期：状态更新计算 live（无缓存则 rebate 也会计算）；赔率更新仅计算 rebate；live 使用缓存不重复计算

说明：非触发信号若无缓存可现场计算，已有缓存则直接复用
