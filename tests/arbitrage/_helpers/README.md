# 测试公共 helper

放置跨 capability 的测试辅助资源。

## 内容

| 路径 | 用途 |
|---|---|
| `debug_configs/debug_config_size_test.json` | 从 `services/integration/` 平移,Q11 实施时作为 DebugConfig fixture |
| `__init__.py` | 包标识 |
| (后续) `fixtures.py` | 公共 pytest fixture(NT TradingNode 工厂、mock instrument 生成器等) |
| (后续) `factories.py` | 测试用 NT 组件构造工厂 |
| (后续) `data/` | HTML 快照 / WS 帧示例 / API 响应样本等 |

## 约定

- 任何被 ≥2 个 capability 用到的 fixture / 工厂 / 数据都搬到这里
- 单一 capability 用的 helper 留在该 capability 自己的目录(避免本目录过载)
