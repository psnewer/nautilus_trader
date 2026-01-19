# -*- coding: utf-8 -*-
"""
通用工具函数模块

提供字符串相似度计算等通用功能，供市场发现和市场匹配模块使用。
"""

import re
from typing import List, Tuple


def is_subsequence(s: str, subseq: str) -> bool:
    """
    检查 subseq 是否是 s 的子序列

    Args:
        s: 主字符串
        subseq: 要检查的子序列

    Returns:
        如果 subseq 是 s 的子序列返回 True

    Example:
        is_subsequence("English", "Eglsh") -> True
        is_subsequence("Premier", "Pmir") -> True
    """
    j = 0  # subseq 的索引

    # 遍历 s 的每个字符
    for i in range(len(s)):
        if j >= len(subseq):
            break
        if s[i] == subseq[j]:
            j += 1  # 当字符匹配时，移动 subseq 的索引

    # 如果 subseq 的所有字符都被找到，返回 True
    return j == len(subseq)


def split_elements(text: str, keep_unicode: bool = True) -> List[str]:
    """
    拆分字符串为元素

    Args:
        text: 要拆分的字符串
        keep_unicode: 是否保留 Unicode 字母（如 é, ñ, ü）

    Returns:
        拆分后的元素列表
    """
    if keep_unicode:
        # 只按常见分隔符拆分，保留 Unicode 字母
        parts = re.split(r'[\s\-_.,;:!?/\\@#$%^&*()[\]{}|<>"\'+]+', text)
    else:
        # 按非字母数字字符拆分
        parts = re.split(r'[^a-zA-Z0-9]', text)

    return [p for p in parts if p]


def split_elements_for_discovery(text: str) -> List[str]:
    """
    拆分字符串为元素 (用于市场发现)

    当字符串中有 >=2 个大写字母时，将其拆分为单个字母
    例如: "EPL" -> ['E', 'P', 'L']
         "English" -> ['English']

    Args:
        text: 要拆分的字符串

    Returns:
        拆分后的元素列表
    """
    parts = re.split(r'[^a-zA-Z0-9]', text)
    elements = []

    for part in parts:
        if not part:
            continue

        upper_count = sum(1 for c in part if c.isupper())

        if upper_count >= 2:
            # 缩写词拆分为单个字母
            elements.extend(list(part))
        else:
            elements.append(part)

    return elements


def match_abbreviation(full_name: str, abbrev: str) -> bool:
    """
    检查缩写是否匹配全名

    Args:
        full_name: 全名 (如 "English Premier League")
        abbrev: 缩写 (如 "EPL")

    Returns:
        True 如果缩写匹配全名

    Example:
        match_abbreviation("English Premier League", "EPL") -> True
        match_abbreviation("National Football League", "NFL") -> True
    """
    # 检查是否是缩写 (全大写且长度 >= 2)
    if not abbrev.isupper() or len(abbrev) < 2:
        return False

    # 拆分全名为单词
    words = split_elements(full_name, keep_unicode=True)

    # 缩写的每个字母应该匹配单词的首字母
    abbrev_idx = 0
    for word in words:
        if abbrev_idx >= len(abbrev):
            break
        if word and word[0].upper() == abbrev[abbrev_idx]:
            abbrev_idx += 1

    return abbrev_idx == len(abbrev)


def get_similar_for_matching(shorten: bool, base: str, *args: str) -> int:
    """
    计算相似度 (用于市场匹配)

    Args:
        shorten: True 使用子序列匹配, False 使用包含匹配
        base: 基准字符串
        *args: 要比较的字符串列表

    Returns:
        匹配的元素总数，如果任一参数匹配数为0则返回0

    Example:
        get_similar_for_matching(True, "Chelsea", "Chelsea FC") -> 1
        get_similar_for_matching(True, "English Premier League", "EPL") -> 3
    """
    # 将基准字符串拆分成元素
    base_elements = split_elements(base, keep_unicode=True)

    if not base_elements:
        return 0

    should_return_zero = False
    total_included = 0

    for arg in args:
        # 将当前参数拆分成元素
        arg_elements = split_elements(arg, keep_unicode=True)

        if not arg_elements:
            should_return_zero = True
            continue

        # 先尝试缩写匹配 (如 EPL vs English Premier League)
        # 尝试 arg 是 base 的缩写 (arg 只有一个元素时)
        if len(arg_elements) == 1:
            arg_single = arg_elements[0]
            if match_abbreviation(base, arg_single):
                total_included += len(base_elements)
                continue

        # 尝试 base 是 arg 的缩写 (base 只有一个元素时)
        if len(base_elements) == 1:
            base_single = base_elements[0]
            if match_abbreviation(arg, base_single):
                total_included += len(arg_elements)
                continue

        # 计算当前参数中有多少元素被基准参数包含
        # 使用集合跟踪已匹配的 base 元素，防止重复匹配
        used_base_indices = set()
        included_count = 0

        for elem in arg_elements:
            if len(elem) <= 1:
                continue  # 跳过单字符元素

            for base_idx, base_elem in enumerate(base_elements):
                if base_idx in used_base_indices:
                    continue  # 跳过已使用的 base 元素

                if len(base_elem) <= 1:
                    continue  # 跳过单字符元素

                if shorten:
                    # 使用子序列匹配
                    if is_subsequence(base_elem, elem) or is_subsequence(elem, base_elem):
                        included_count += 1
                        used_base_indices.add(base_idx)  # 标记为已使用
                        break
                else:
                    # 使用包含匹配
                    if base_elem in elem or elem in base_elem:
                        included_count += 1
                        used_base_indices.add(base_idx)  # 标记为已使用
                        break

        # 如果任何一个参数的匹配元素数量为0，则返回0
        if included_count == 0:
            should_return_zero = True

        total_included += included_count

    return 0 if should_return_zero else total_included


def get_similar_for_discovery(str1: str, str2: str) -> Tuple[int, int]:
    """
    相似度计算 (用于市场发现，保持原有逻辑)

    Args:
        str1: 第一个字符串
        str2: 第二个字符串

    Returns:
        (匹配数, 字符数) 元组
    """
    elements1 = split_elements_for_discovery(str1)
    elements2 = split_elements_for_discovery(str2)

    if not elements1 or not elements2:
        return (0, 0)

    # 提取大写字母
    upper_letters1 = [e for e in elements1 if len(e) == 1 and e.isupper()]
    upper_letters2 = [e for e in elements2 if len(e) == 1 and e.isupper()]

    # 检查大写字母匹配
    if upper_letters1:
        for letter in upper_letters1:
            matched = False
            for e2 in elements2:
                if len(e2) == 1 and e2.isupper():
                    if letter == e2:
                        matched = True
                        break
                else:
                    if e2 and e2[0] == letter:
                        matched = True
                        break
            if not matched:
                return (0, 0)

    if upper_letters2:
        for letter in upper_letters2:
            matched = False
            for e1 in elements1:
                if len(e1) == 1 and e1.isupper():
                    if letter == e1:
                        matched = True
                        break
                else:
                    if e1 and e1[0] == letter:
                        matched = True
                        break
            if not matched:
                return (0, 0)

    matches = 0
    char_count = 0
    used_indices2 = set()

    for e1 in elements1:
        for idx2, e2 in enumerate(elements2):
            if idx2 in used_indices2:
                continue

            matched = False
            matched_chars = 0

            if len(e1) == 1 and e1.isupper():
                if len(e2) == 1 and e1 == e2:
                    matched = True
                    matched_chars = 1
                elif len(e2) > 1 and e2[0] == e1:
                    matched = True
                    matched_chars = 1
            elif len(e2) == 1 and e2.isupper():
                if len(e1) > 1 and e1[0] == e2:
                    matched = True
                    matched_chars = 1
            else:
                if is_subsequence(e2, e1):
                    matched = True
                    matched_chars = len(e1)
                elif is_subsequence(e1, e2):
                    matched = True
                    matched_chars = len(e2)

            if matched:
                matches += 1
                char_count += matched_chars
                used_indices2.add(idx2)
                break

    return (matches, char_count)
