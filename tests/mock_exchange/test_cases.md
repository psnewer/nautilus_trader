# 模拟交易所测试用例

## 用例 1：模拟订单状态流转

- 前置条件：启用 Debug 模式，开启 `use_mock_exchange` 覆盖，配置 execution mock timeline
- 操作步骤：
  1. 创建 Polymarket 订单并调用模拟下单
  2. 等待状态流转完成
- 预期结果：
  - 订单状态依次经历 live、partially_filled、filled
  - 最终成交数量与订单 size 一致

## 用例 2：执行服务走模拟交易所

- 前置条件：启用 Debug 模式，开启 `use_mock_exchange` 覆盖，配置 execution mock 为拒绝
- 操作步骤：
  1. 创建 Polymarket 订单并设置 `test_scenario=reject`
  2. 调用执行服务下单
- 预期结果：
  - 执行结果为失败
  - 订单状态为 rejected

## 用例 3：模拟撤单

- 前置条件：启用 Debug 模式，开启 `use_mock_exchange` 覆盖，配置 execution mock timeline
- 操作步骤：
  1. 创建 Polymarket 订单并调用模拟下单
  2. 立即发起撤单
- 预期结果：
  - 撤单成功
  - 订单状态为 cancelled
  - 订单未产生成交
