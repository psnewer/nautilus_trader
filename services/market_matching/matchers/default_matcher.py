"""
Default Matcher - 默认匹配器
使用 getSimilar 算法进行智能匹配
"""

import re
from typing import List, Tuple


class DefaultMatcher:
    """
    默认匹配器
    使用 getSimilar 算法匹配 sport, competition, home_team, away_team
    """
    
    @staticmethod
    def _split_elements(text: str) -> List[str]:
        """
        按非字母数字字符拆分文本
        如果部分有 2 个大写字母  拆成单字符
        否则保留整个部分
        
        示例:
        - "EPL"  ['E', 'P', 'L']
        - "LoL"  ['L', 'o', 'L']
        - "League of Legends"  ['League', 'of', 'Legends']
        """
        # 按非字母数字字符拆分
        parts = re.split(r'[^a-zA-Z0-9]+', text)
        parts = [p for p in parts if p]  # 去掉空字符串
        
        result = []
        for part in parts:
            # 统计大写字母数量
            uppercase_count = sum(1 for c in part if c.isupper())
            
            if uppercase_count >= 2:
                # 拆成单字符
                result.extend(list(part))
            else:
                result.append(part)
        
        return result
    
    @staticmethod
    def _is_subsequence(short: str, long: str) -> bool:
        """检查 short 是否是 long 的子序列"""
        short = short.lower()
        long = long.lower()
        
        it = iter(long)
        return all(c in it for c in short)
    
    @staticmethod
    def _check_uppercase_letters(str1_parts: List[str], str2_parts: List[str]) -> bool:
        """
        检查所有单个大写字母是否全部匹配
        
        单个大写字母可以匹配:
        a) 相同的单个大写字母: E == E
        b) 单词的首字母: E == English[0]
        
        如果任何一个大写字母没匹配上  返回 False
        """
        # 找出 str1 中的所有单个大写字母
        uppercase_letters = [p for p in str1_parts if len(p) == 1 and p.isupper()]
        
        if not uppercase_letters:
            return True  # 没有单个大写字母，跳过检查
        
        # 检查每个大写字母是否能匹配
        for letter in uppercase_letters:
            matched = False
            
            for part in str2_parts:
                # 匹配相同的单个大写字母
                if len(part) == 1 and part.upper() == letter:
                    matched = True
                    break
                
                # 匹配单词的首字母
                if len(part) > 1 and part[0].upper() == letter:
                    matched = True
                    break
            
            if not matched:
                return False  # 有大写字母没匹配上
        
        return True
    
    @classmethod
    def get_similar(cls, str1: str, str2: str) -> Tuple[int, int]:
        """
        计算两个字符串的相似度
        
        返回: (match_count, char_count)
        - match_count: 匹配的元素数量
        - char_count: 匹配的字符总数
        
        比较优先级:
        1. 先比 match_count (越大越好)
        2. match_count 相同时比 char_count (越大越好)
        """
        str1_parts = cls._split_elements(str1)
        str2_parts = cls._split_elements(str2)
        
        # 检查大写字母
        if not cls._check_uppercase_letters(str1_parts, str2_parts):
            return (0, 0)
        
        # 计算匹配
        match_count = 0
        char_count = 0
        used_indices = set()  # 记录 str2 中已使用的元素
        
        for part1 in str1_parts:
            matched = False
            
            for i, part2 in enumerate(str2_parts):
                if i in used_indices:
                    continue  # 已被使用，跳过
                
                # 1. 完全相同
                if part1.lower() == part2.lower():
                    match_count += 1
                    char_count += len(part1)
                    used_indices.add(i)
                    matched = True
                    break
                
                # 2. part1 是 part2 的首字母
                if len(part1) == 1 and len(part2) > 1:
                    if part1.upper() == part2[0].upper():
                        match_count += 1
                        char_count += 1
                        used_indices.add(i)
                        matched = True
                        break
                
                # 3. part1 是 part2 的子序列
                if cls._is_subsequence(part1, part2):
                    match_count += 1
                    char_count += len(part1)
                    used_indices.add(i)
                    matched = True
                    break
        
        return (match_count, char_count)
    
    def match(self, poly_event: dict, orbit_event: dict, config: dict) -> Tuple[bool, dict]:
        """
        匹配两个事件
        
        Args:
            poly_event: Polymarket 事件
            orbit_event: OrbitExch 事件
            config: 匹配配置
        
        Returns:
            (is_match, scores): 是否匹配和各字段的匹配分数
        """
        scores = {}
        
        # 1. 匹配 sport
        if config.get('match_sport', True):
            score = self.get_similar(
                poly_event['sport'],
                orbit_event['sport']
            )
            scores['sport'] = score
            
            if score == (0, 0):
                return False, scores
        
        # 2. 匹配 competition
        if config.get('match_competition', True):
            score = self.get_similar(
                poly_event['competition'],
                orbit_event['competition']
            )
            scores['competition'] = score
            
            if score == (0, 0):
                return False, scores
        
        # 3. 匹配 home_team 和 away_team
        if config.get('match_home_team', True) and config.get('match_away_team', True):
            # 正常顺序匹配
            home_score = self.get_similar(
                poly_event['home_team'],
                orbit_event['home_team']
            )
            away_score = self.get_similar(
                poly_event['away_team'],
                orbit_event['away_team']
            )
            
            normal_match = (home_score != (0, 0) and away_score != (0, 0))
            
            # 如果允许交换，尝试交换顺序
            if config.get('allow_team_swap', True) and not normal_match:
                home_score_swap = self.get_similar(
                    poly_event['home_team'],
                    orbit_event['away_team']
                )
                away_score_swap = self.get_similar(
                    poly_event['away_team'],
                    orbit_event['home_team']
                )
                
                swap_match = (home_score_swap != (0, 0) and away_score_swap != (0, 0))
                
                if swap_match:
                    scores['home_team'] = home_score_swap
                    scores['away_team'] = away_score_swap
                    scores['team_swapped'] = True
                    return True, scores
            
            if normal_match:
                scores['home_team'] = home_score
                scores['away_team'] = away_score
                scores['team_swapped'] = False
                return True, scores
            else:
                scores['home_team'] = home_score
                scores['away_team'] = away_score
                return False, scores
        
        return True, scores
