"""
套利系统通用工具函数模块

本模块提供跨服务共享的工具函数，包括：
- 时间处理
- 数据转换
- 格式化工具
- 验证函数

使用前请查阅 utils_api.md 获取完整 API 文档。
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any


# =============================================================================
# 时间处理函数
# =============================================================================

def utc_now() -> datetime:
    """获取当前 UTC 时间（带时区信息）"""
    return datetime.now(timezone.utc)


def timestamp_ms() -> int:
    """获取当前 UTC 时间戳（毫秒）"""
    return int(utc_now().timestamp() * 1000)


def ms_to_datetime(ms: int) -> datetime:
    """毫秒时间戳转 datetime（UTC）"""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# =============================================================================
# 数值处理函数
# =============================================================================

def round_down(value: float | Decimal, precision: int) -> Decimal:
    """
    向下取整到指定精度

    Args:
        value: 待处理数值
        precision: 小数位数

    Returns:
        向下取整后的 Decimal
    """
    d = Decimal(str(value))
    return d.quantize(Decimal(10) ** -precision, rounding=ROUND_DOWN)


def calculate_spread_pct(price_a: float, price_b: float) -> float:
    """
    计算两个价格的价差百分比

    Args:
        price_a: 价格 A
        price_b: 价格 B

    Returns:
        价差百分比（price_b - price_a）/ price_a * 100
    """
    if price_a == 0:
        return 0.0
    return ((price_b - price_a) / price_a) * 100


# =============================================================================
# 字符串处理函数
# =============================================================================

def generate_id(prefix: str = "") -> str:
    """
    生成唯一 ID

    Args:
        prefix: ID 前缀（可选）

    Returns:
        格式：{prefix}-{timestamp_ms}-{random_hex}
    """
    import secrets
    ts = timestamp_ms()
    rand = secrets.token_hex(4)
    if prefix:
        return f"{prefix}-{ts}-{rand}"
    return f"{ts}-{rand}"


def safe_get(data: dict, *keys: str, default: Any = None) -> Any:
    """
    安全获取嵌套字典值

    Args:
        data: 字典数据
        *keys: 键路径
        default: 默认值

    Returns:
        找到的值或默认值

    Example:
        safe_get({"a": {"b": 1}}, "a", "b") -> 1
        safe_get({"a": {"b": 1}}, "a", "c", default=0) -> 0
    """
    result = data
    for key in keys:
        if isinstance(result, dict) and key in result:
            result = result[key]
        else:
            return default
    return result


# =============================================================================
# 字符串匹配函数
# =============================================================================

def is_subsequence(text: str, subseq: str) -> bool:
    """
    检查 subseq 是否为 text 的子序列

    子序列：字符按顺序出现，但不必连续
    例如："ace" 是 "abcde" 的子序列

    Args:
        text: 原始字符串
        subseq: 待检查的子序列

    Returns:
        subseq 是否为 text 的子序列
    """
    j = 0
    for char in text:
        if j < len(subseq) and char == subseq[j]:
            j += 1
    return j == len(subseq)


def get_similar(shorten: bool, base: str, *args: str) -> int:
    """
    计算多个字符串与基准字符串的相似匹配数

    用于跨平台市场匹配，将字符串按非字母数字字符拆分后进行比较。

    Args:
        shorten: True 使用子序列匹配，False 使用子串匹配
        base: 基准字符串
        *args: 待比较的字符串列表

    Returns:
        匹配元素总数，如果任一参数匹配数为 0 则返回 0

    Example:
        get_similar(False, "Man-United", "Manchester United FC")  # 子串匹配
        get_similar(True, "MU", "Man United")  # 子序列匹配
    """
    import re

    # 将基准字符串按非字母数字字符拆分
    base_elements = [elem for elem in re.split(r'[^a-zA-Z0-9]', base) if elem]

    should_return_zero = False
    total_included = 0

    for arg in args:
        # 将当前参数拆分成元素
        arg_elements = [elem for elem in re.split(r'[^a-zA-Z0-9]', arg) if elem]

        # 计算当前参数中有多少元素与基准参数匹配
        included_count = 0
        for elem in arg_elements:
            if len(elem) <= 1:
                continue

            for base_elem in base_elements:
                if len(base_elem) <= 1:
                    continue

                if shorten:
                    # 子序列匹配
                    if is_subsequence(base_elem, elem) or is_subsequence(elem, base_elem):
                        included_count += 1
                        break
                else:
                    # 子串匹配
                    if base_elem in elem or elem in base_elem:
                        included_count += 1
                        break

        # 如果任何一个参数匹配数为 0，则标记返回 0
        if included_count == 0:
            should_return_zero = True

        total_included += included_count

    return 0 if should_return_zero else total_included


# =============================================================================
# 增强版字符串匹配函数 (市场发现专用)
# =============================================================================

def _count_uppercase(text: str) -> int:
    """统计字符串中大写字母数量"""
    return sum(1 for c in text if c.isupper())


def _split_tokens_enhanced(text: str) -> list[str]:
    """
    增强版分词：按非字母数字拆分，并对含 >=2 个大写字母的 token 逐字符拆分

    规则：
    - 按非字母数字字符拆分
    - 如果某个 token 含有 >=2 个大写字母，拆分为逐字符（保留大小写）
    - 例如：EPL → ["E", "P", "L"]，LoL → ["L", "o", "L"]

    Args:
        text: 输入字符串

    Returns:
        分词后的 token 列表
    """
    import re
    raw_tokens = [t for t in re.split(r'[^a-zA-Z0-9]', text) if t]

    expanded = []
    for token in raw_tokens:
        if _count_uppercase(token) >= 2:
            # 逐字符拆分
            expanded.extend(list(token))
        else:
            expanded.append(token)

    return expanded


def _token_match(base_token: str, cand_token: str) -> tuple[bool, int]:
    """
    检查两个 token 是否匹配，返回 (是否匹配, 匹配字符数)

    规则：
    - 不做大小写转换
    - 子串匹配或子序列匹配

    Args:
        base_token: 基准 token
        cand_token: 候选 token

    Returns:
        (是否匹配, 匹配的字符数)
    """
    # 子串匹配
    if base_token in cand_token:
        return (True, len(base_token))
    if cand_token in base_token:
        return (True, len(cand_token))

    # 子序列匹配
    if is_subsequence(base_token, cand_token):
        return (True, len(cand_token))
    if is_subsequence(cand_token, base_token):
        return (True, len(base_token))

    return (False, 0)


def get_similar_enhanced(base: str, candidate: str) -> tuple[int, int, bool]:
    """
    增强版相似度匹配，支持大写字母拆分和全匹配规则

    规则：
    1. 不做大小写转换
    2. 含 >=2 个大写字母的 token 拆分为逐字符
    3. 已匹配成功的 token 不再参与后续匹配
    4. 单个大写字母必须全部匹配成功
    5. 返回值 >0 即匹配成功

    Args:
        base: 基准字符串（如 "English Premier League"）
        candidate: 候选字符串（如 "EPL"）

    Returns:
        (score, matched_chars, is_match)
        - score: 匹配分数（匹配的 token 数）
        - matched_chars: 匹配的总字符数
        - is_match: 是否匹配成功

    Example:
        get_similar_enhanced("English Premier League", "EPL")
        # E 匹配 English, P 匹配 Premier, L 匹配 League
        # 返回 (3, 3, True)
    """
    base_tokens = _split_tokens_enhanced(base)
    cand_tokens = _split_tokens_enhanced(candidate)

    used_base: set[int] = set()  # 已使用的 base token 索引
    total_score = 0
    total_chars = 0

    # 识别单个大写字母
    single_upper_indices = [
        i for i, t in enumerate(cand_tokens)
        if len(t) == 1 and t.isupper()
    ]

    # 规则：单个大写字母必须全部匹配
    for cand_idx in single_upper_indices:
        cand_token = cand_tokens[cand_idx]
        matched = False

        for base_idx, base_token in enumerate(base_tokens):
            if base_idx in used_base:
                continue

            # 单个大写字母匹配规则：必须是 base_token 的首字母
            if base_token and base_token[0] == cand_token:
                used_base.add(base_idx)
                total_score += 1
                total_chars += 1
                matched = True
                break

        if not matched:
            # 单个大写字母未匹配，整体失败
            return (0, 0, False)

    # 处理其他 tokens
    for cand_idx, cand_token in enumerate(cand_tokens):
        if cand_idx in single_upper_indices:
            continue  # 已处理

        if not cand_token:
            continue

        best_score = 0
        best_chars = 0
        best_base_idx = -1

        for base_idx, base_token in enumerate(base_tokens):
            if base_idx in used_base:
                continue

            if not base_token:
                continue

            is_match, chars = _token_match(base_token, cand_token)
            if is_match:
                # 选择最佳匹配
                if chars > best_chars or (chars == best_chars and best_base_idx == -1):
                    best_score = 1
                    best_chars = chars
                    best_base_idx = base_idx

        if best_score > 0 and best_base_idx >= 0:
            used_base.add(best_base_idx)
            total_score += best_score
            total_chars += best_chars

    return (total_score, total_chars, total_score > 0)


# =============================================================================
# Size 门控函数
# =============================================================================

# 各平台最小可交易 size
# OrbitExch: stake（下注金额）>= 7
# Polymarket: share（份额）>= 5
MIN_SIZE_ORBITEXCH = 7.0
MIN_SIZE_POLYMARKET = 5.0


def check_min_size(venue: str, size: float) -> bool:
    """
    检查订单 size 是否满足平台最小要求

    Args:
        venue: 平台 ("polymarket" | "orbitexch")
        size: Polymarket 为 share（份额），OrbitExch 为 stake（下注金额）

    Returns:
        True 表示满足最小要求
    """
    if venue == "orbitexch":
        return size >= MIN_SIZE_ORBITEXCH
    elif venue == "polymarket":
        return size >= MIN_SIZE_POLYMARKET
    return True


def check_all_legs_min_size(legs: list[dict]) -> bool:
    """
    检查所有腿是否都满足最小 size 要求

    Args:
        legs: [{"venue": str, "size": float}, ...]

    Returns:
        True 表示全部满足，False 表示至少有一条腿不满足
    """
    for leg in legs:
        venue = leg.get("venue", "")
        size = leg.get("size", 0)
        if not check_min_size(venue, size):
            return False
    return True


def adjust_share_by_liquidity(
    share: float,
    legs: list[dict],
    fx: float = 1.0,
) -> float | None:
    """
    根据市场可交易量调整 share 并检查最小 size 门控

    Step 1 — 缩放：如果某条腿的市场可交易量 < 计划下注量，按比例缩放所有腿
    Step 2 — 门控：缩放后检查每条腿是否满足平台最小 size

    无可用量数据时（available=0）不阻止。

    Args:
        share: 原始 share（基准金额，USD）
        legs: 每条腿的信息列表，每项包含:
            - venue: str ("polymarket" | "orbitexch")
            - intended: float (计划下注量，Polymarket=share，OrbitExch=stake)
            - available: float (市场可交易量)
            - raw_odds: float (OrbitExch 赔率，用于门控时换算 stake)
        fx: GBP/USD 汇率（OrbitExch stake 需要除以 fx 转为 GBP）

    Returns:
        调整后的 share，如果最小值不满足返回 None
    """
    scale_factor = 1.0

    # Step 1: 按可交易量等比例缩放
    for leg in legs:
        intended = leg.get("intended", 0)
        available = leg.get("available", 0)

        if intended > 0 and available > 0 and available < intended:
            ratio = available / intended
            scale_factor = min(scale_factor, ratio)

    adjusted_share = share * scale_factor

    # Step 2: 最小 size 门控
    for leg in legs:
        venue = leg.get("venue", "")
        if venue == "polymarket":
            leg_size = adjusted_share
        else:  # orbitexch
            raw_odds = leg.get("raw_odds", 0)
            # OrbitExch stake = share / odds / fx (GBP)
            leg_size = adjusted_share / raw_odds / fx if raw_odds > 0 else 0

        if not check_min_size(venue, leg_size):
            return None

    return adjusted_share


def select_best_match(
    base_list: list[str],
    candidate: str,
) -> str | None:
    """
    从候选列表中选择最佳匹配项

    规则：
    1. 先尝试完全相同匹配
    2. 否则使用 get_similar_enhanced 选择最高分

    Args:
        base_list: 基准字符串列表
        candidate: 候选字符串

    Returns:
        最佳匹配的基准字符串，或 None
    """
    # 完全匹配优先
    if candidate in base_list:
        return candidate

    best_match = None
    best_score = 0
    best_chars = 0

    for base in base_list:
        score, chars, is_match = get_similar_enhanced(base, candidate)

        if not is_match:
            continue

        # 比较：先比 score，再比 chars
        if score > best_score or (score == best_score and chars > best_chars):
            best_match = base
            best_score = score
            best_chars = chars

    return best_match
