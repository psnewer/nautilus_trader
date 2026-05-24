# Strategy 组件详细设计(搁置)

> **状态:搁置**(用户 2026-05-21 决定暂不展开)。Strategy 是最复杂组件,初设 `refactor.md §5.4` 已积累一批约束,但**核心信号流水线尚未设计**。
> 对应初设 Step 4。

## 已锁定的约束(散见 refactor.md,详化时汇总到本文)

| 主题 | 锁定点 | 出处 |
|---|---|---|
| 边界 | 只管决策;**不引用 Risk**(透明拦截);拥有深度缩放 | §5.4 |
| 每轮重算 | 每轮全量重算意图,不缓存待下单 | Q13 / strategy-4.9 |
| settled pre-check | 调 way_rebate / submit 前判 `leg_settled`(三分支) | Q-G / strategy-4.14 |
| 调 Portfolio | `portfolio.min_way_rebate(pair)` 判机会,pull-based | Q14 / strategy-4.12 |
| tp/sl 归 Risk | 止盈/止损/全局熔断不在 strategy(归 RiskEngine) | Q16 |
| 健康检查互斥 | submit 前 `if _health_check_active: 放弃`(前置 pre-check) | Q19 / §6.10 / strategy-4.15 |
| 机会快照 | 评估开跑冻 per-pair 快照(订单簿+持仓+way_rebate),全程用拷贝;回收放 finally | Q20 / strategy-4.{17,18,19} |

## 详化时还需设计(已识别的大 gap)

**核心未设计 = 可插拔信号流水线 + 配置驱动**(2026-05-21 从 requirements 发现,refactor.md 未捕捉):
- signal 插件(rebate / multi-way / mean_signal / pre-match / live)+ params
- strategy = 有序信号组合,任一返 false 中止;共享"套利数组"(signal 读写)
- 父类默认方向选择(优先级:已获返水率负 > 已获返水率最小 > fresh rebate 最大)+ 子类可覆盖
- 概率转换(OE 100/odds、PM ×100)+ 互斥概率求和 + 返水率算法
- mean_signal 的 discount sizing 公式
- competition / match 级配置查找与覆盖

> 详细设计待用户启动 Strategy 时进行;届时按标准模板(7 节)+ 上面的信号框架展开。
