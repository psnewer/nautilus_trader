# Utils API 文档

**模块路径**：`src/arbitrage/common/utils.py`

**最后更新**：2026-02-22

---

## 时间处理函数

### `utc_now() -> datetime`

获取当前 UTC 时间（带时区信息）

```python
from src.arbitrage.common.utils import utc_now

now = utc_now()
# datetime.datetime(2026, 1, 20, 12, 30, 45, tzinfo=datetime.timezone.utc)
```

---

### `timestamp_ms() -> int`

获取当前 UTC 时间戳（毫秒）

```python
from src.arbitrage.common.utils import timestamp_ms

ts = timestamp_ms()
# 1737376245123
```

---

### `ms_to_datetime(ms: int) -> datetime`

毫秒时间戳转 datetime（UTC）

| 参数 | 类型 | 说明 |
|------|------|------|
| ms | int | 毫秒时间戳 |

```python
from src.arbitrage.common.utils import ms_to_datetime

dt = ms_to_datetime(1737376245123)
# datetime.datetime(2026, 1, 20, 12, 30, 45, 123000, tzinfo=datetime.timezone.utc)
```

---

## 数值处理函数

### `round_down(value: float | Decimal, precision: int) -> Decimal`

向下取整到指定精度

| 参数 | 类型 | 说明 |
|------|------|------|
| value | float \| Decimal | 待处理数值 |
| precision | int | 小数位数 |

```python
from src.arbitrage.common.utils import round_down

result = round_down(1.23456, 2)
# Decimal('1.23')

result = round_down(1.999, 2)
# Decimal('1.99')
```

---

### `calculate_spread_pct(price_a: float, price_b: float) -> float`

计算两个价格的价差百分比

| 参数 | 类型 | 说明 |
|------|------|------|
| price_a | float | 价格 A（基准价） |
| price_b | float | 价格 B |

**返回值**：`(price_b - price_a) / price_a * 100`

```python
from src.arbitrage.common.utils import calculate_spread_pct

spread = calculate_spread_pct(100, 102)
# 2.0 (%)

spread = calculate_spread_pct(100, 98)
# -2.0 (%)
```

---

## 字符串处理函数

### `generate_id(prefix: str = "") -> str`

生成唯一 ID

| 参数 | 类型 | 说明 |
|------|------|------|
| prefix | str | ID 前缀（可选） |

**返回格式**：`{prefix}-{timestamp_ms}-{random_hex}`

```python
from src.arbitrage.common.utils import generate_id

order_id = generate_id("ORD")
# "ORD-1737376245123-a1b2c3d4"

simple_id = generate_id()
# "1737376245123-a1b2c3d4"
```

---

### `safe_get(data: dict, *keys: str, default: Any = None) -> Any`

安全获取嵌套字典值

| 参数 | 类型 | 说明 |
|------|------|------|
| data | dict | 字典数据 |
| *keys | str | 键路径（可变参数） |
| default | Any | 默认值（默认 None） |

```python
from src.arbitrage.common.utils import safe_get

data = {"a": {"b": {"c": 1}}}

value = safe_get(data, "a", "b", "c")
# 1

value = safe_get(data, "a", "x", default=0)
# 0

value = safe_get(data, "missing")
# None
```

---

## 字符串匹配函数

### `is_subsequence(text: str, subseq: str) -> bool`

检查 subseq 是否为 text 的子序列（字符按顺序出现，但不必连续）

| 参数 | 类型 | 说明 |
|------|------|------|
| text | str | 原始字符串 |
| subseq | str | 待检查的子序列 |

```python
from src.arbitrage.common.utils import is_subsequence

is_subsequence("abcde", "ace")
# True（a-c-e 按顺序出现）

is_subsequence("abcde", "aec")
# False（e 在 c 之前）
```

---

### `get_similar(shorten: bool, base: str, *args: str) -> int`

计算多个字符串与基准字符串的相似匹配数，用于跨平台市场匹配

| 参数 | 类型 | 说明 |
|------|------|------|
| shorten | bool | True 使用子序列匹配，False 使用子串匹配 |
| base | str | 基准字符串 |
| *args | str | 待比较的字符串列表 |

**返回值**：匹配元素总数，如果任一参数匹配数为 0 则返回 0

**匹配规则**：
1. 将字符串按非字母数字字符拆分为元素
2. 忽略长度 ≤1 的元素
3. `shorten=False`：子串匹配（A 包含 B 或 B 包含 A）
4. `shorten=True`：子序列匹配（更宽松）

```python
from src.arbitrage.common.utils import get_similar

# 子串匹配模式
get_similar(False, "Man-United", "Manchester United FC")
# 2（Man 匹配 Manchester，United 匹配 United）

get_similar(False, "Real-Madrid", "Barcelona FC")
# 0（无匹配，返回 0）

# 子序列匹配模式（更宽松）
get_similar(True, "MU-vs-RM", "Man United", "Real Madrid")
# 匹配缩写形式
```

---

## 函数索引

| 函数名 | 分类 | 简述 |
|--------|------|------|
| `utc_now` | 时间 | 获取当前 UTC 时间 |
| `timestamp_ms` | 时间 | 获取毫秒时间戳 |
| `ms_to_datetime` | 时间 | 毫秒转 datetime |
| `round_down` | 数值 | 向下取整 |
| `calculate_spread_pct` | 数值 | 计算价差百分比 |
| `generate_id` | 字符串 | 生成唯一 ID |
| `safe_get` | 字符串 | 安全获取嵌套值 |
| `is_subsequence` | 匹配 | 检查子序列 |
| `get_similar` | 匹配 | 跨平台市场匹配 |
