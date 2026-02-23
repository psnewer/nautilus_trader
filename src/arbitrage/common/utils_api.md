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

## Size 门控函数

### 常量

| 常量 | 值 | 说明 |
|------|------|------|
| `MIN_SIZE_ORBITEXCH` | 7.0 | OrbitExch 最小 stake |
| `MIN_SIZE_POLYMARKET` | 5.0 | Polymarket 最小 share |

### `check_min_size(venue: str, size: float) -> bool`

检查订单 size 是否满足平台最小要求

| 参数 | 类型 | 说明 |
|------|------|------|
| venue | str | 平台 ("polymarket" \| "orbitexch") |
| size | float | 订单 size |

```python
from src.arbitrage.common.utils import check_min_size

check_min_size("orbitexch", 6)   # False
check_min_size("orbitexch", 7)   # True
check_min_size("polymarket", 5)  # True
```

---

### `check_all_legs_min_size(legs: list[dict]) -> bool`

检查所有腿是否都满足最小 size 要求

| 参数 | 类型 | 说明 |
|------|------|------|
| legs | list[dict] | `[{"venue": str, "size": float}, ...]` |

```python
from src.arbitrage.common.utils import check_all_legs_min_size

check_all_legs_min_size([
    {"venue": "polymarket", "size": 10},
    {"venue": "orbitexch", "size": 8},
])  # True

check_all_legs_min_size([
    {"venue": "polymarket", "size": 10},
    {"venue": "orbitexch", "size": 5},
])  # False (OrbitExch < 7)
```

---

### `adjust_share_by_liquidity(share: float, legs: list[dict]) -> float | None`

根据市场可交易量调整 share 并检查最小 size 门控

**Step 1 — 缩放**：如果某条腿的 available < intended，按最小比例缩放所有腿
**Step 2 — 门控**：缩放后检查每条腿是否满足平台最小 size

无可用量数据时（available=0）不阻止。

| 参数 | 类型 | 说明 |
|------|------|------|
| share | float | 原始 share（基准金额） |
| legs | list[dict] | 每条腿信息，含 venue, intended, available, raw_odds |

**返回值**：调整后的 share，如果最小值不满足返回 None

```python
from src.arbitrage.common.utils import adjust_share_by_liquidity

# 充足流动性 → 不缩放
result = adjust_share_by_liquidity(15.0, [
    {"venue": "polymarket", "intended": 15, "available": 500, "raw_odds": 0},
    {"venue": "orbitexch", "intended": 8.24, "available": 200, "raw_odds": 1.82},
])
# 15.0

# 流动性不足 → 缩放后满足最小值
result = adjust_share_by_liquidity(15.0, [
    {"venue": "polymarket", "intended": 15, "available": 10, "raw_odds": 0},
    {"venue": "orbitexch", "intended": 12.5, "available": 200, "raw_odds": 1.20},
])
# 10.0 (scale_factor=10/15=0.667)

# 缩放后不满足最小值 → None
result = adjust_share_by_liquidity(15.0, [
    {"venue": "polymarket", "intended": 15, "available": 3, "raw_odds": 0},
    {"venue": "orbitexch", "intended": 8.24, "available": 200, "raw_odds": 1.82},
])
# None (poly share=3 < MIN_SIZE_POLYMARKET=5)
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
| `check_min_size` | Size 门控 | 检查单平台最小 size |
| `check_all_legs_min_size` | Size 门控 | 检查所有腿最小 size |
| `adjust_share_by_liquidity` | Size 门控 | 按流动性缩放 share 并门控 |
