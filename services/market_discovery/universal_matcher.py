# -------------------------------------------------------------------------------------------------
#  通用市场匹配器
# -------------------------------------------------------------------------------------------------

"""
通用的跨平台市场匹配器

匹配规则:
1. Sport 和 Competition 至少有一个匹配成功
2. Event 必须匹配成功
3. Event 匹配使用主队/客队名称匹配
"""

import logging
import re
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass


@dataclass
class MatchResult:
    """匹配结果"""
    platform1_event: Dict
    platform2_event: Dict
    sport_match: bool
    competition_match: bool
    event_similarity: int
    home_similarity: int
    away_similarity: int
    
    @property
    def is_valid_match(self) -> bool:
        """是否是有效匹配"""
        # Sport 或 Competition 至少一个匹配 AND Event 匹配
        return (self.sport_match or self.competition_match) and (self.home_similarity > 0 and self.away_similarity > 0)
    
    def __repr__(self):
        status = "✅" if self.is_valid_match else "❌"
        return f'{status} Match(sport={self.sport_match}, comp={self.competition_match}, home={self.home_similarity}, away={self.away_similarity})'


class UniversalMatcher:
    """通用市场匹配器"""
    
    def __init__(self):
        self._log = logging.getLogger('UniversalMatcher')
    
    def match_events(
        self,
        events1: List[Dict],
        events2: List[Dict],
        platform1_name: str = "Platform1",
        platform2_name: str = "Platform2"
    ) -> List[MatchResult]:
        """
        匹配两个平台的事件
        
        Parameters
        ----------
        events1 : List[Dict]
            平台1的事件列表 (需要包含: sport, competition, event)
        events2 : List[Dict]
            平台2的事件列表
        platform1_name : str
            平台1名称
        platform2_name : str
            平台2名称
            
        Returns
        -------
        List[MatchResult]
            匹配结果列表（只包含有效匹配）
        """
        self._log.info(f'🔗 匹配事件: {len(events1)} {platform1_name} ↔ {len(events2)} {platform2_name}')
        
        valid_matches = []
        
        for event1 in events1:
            for event2 in events2:
                result = self._match_single_event(event1, event2)
                
                if result.is_valid_match:
                    valid_matches.append(result)
                    self._log.info(
                        f'   ✅ {event1.get("event", "?")} ↔ {event2.get("event", "?")} '
                        f'(home={result.home_similarity}, away={result.away_similarity})'
                    )
        
        self._log.info(f'✅ 找到 {len(valid_matches)} 个有效匹配')
        return valid_matches
    
    def _match_single_event(self, event1: Dict, event2: Dict) -> MatchResult:
        """匹配单个事件"""
        
        # 1. Sport 匹配
        sport_match = self._match_sport(
            event1.get('sport', ''),
            event2.get('sport', '')
        )
        
        # 2. Competition 匹配
        competition_match = self._match_competition(
            event1.get('competition', ''),
            event2.get('competition', '')
        )
        
        # 3. Event 匹配（提取主队/客队）
        home1, away1 = self._extract_teams(event1.get('event', ''))
        home2, away2 = self._extract_teams(event2.get('event', ''))
        
        # 计算主队和客队的相似度
        home_similarity = self._get_similar(True, home1, home2) if home1 and home2 else 0
        away_similarity = self._get_similar(True, away1, away2) if away1 and away2 else 0
        
        return MatchResult(
            platform1_event=event1,
            platform2_event=event2,
            sport_match=sport_match,
            competition_match=competition_match,
            event_similarity=home_similarity + away_similarity,
            home_similarity=home_similarity,
            away_similarity=away_similarity,
        )
    
    def _match_sport(self, sport1: str, sport2: str) -> bool:
        """匹配运动类型"""
        if not sport1 or not sport2:
            return False
        
        # 标准化
        s1 = sport1.lower().strip()
        s2 = sport2.lower().strip()
        
        # 直接匹配
        if s1 == s2:
            return True
        
        # 包含匹配
        if s1 in s2 or s2 in s1:
            return True
        
        # 常见别名
        aliases = {
            'soccer': ['football'],
            'american football': ['nfl', 'football'],
            'basketball': ['nba'],
        }
        
        for key, values in aliases.items():
            if (s1 == key and s2 in values) or (s2 == key and s1 in values):
                return True
        
        return False
    
    def _match_competition(self, comp1: str, comp2: str) -> bool:
        """匹配赛事"""
        if not comp1 or not comp2:
            return False
        
        # 使用相似度函数
        similarity = self._get_similar(True, comp1, comp2)
        return similarity > 0
    
    def _extract_teams(self, event_name: str) -> Tuple[str, str]:
        """
        从事件名称中提取主队和客队
        
        支持格式:
        - "Team A v Team B"
        - "Team A vs Team B"
        - "Team A @ Team B"
        - "Team A - Team B"
        
        Returns
        -------
        Tuple[str, str]
            (home_team, away_team)
        """
        if not event_name:
            return '', ''
        
        # 常见分隔符
        separators = [' v ', ' vs ', ' @ ', ' - ', ' at ']
        
        for sep in separators:
            if sep in event_name.lower():
                parts = event_name.lower().split(sep)
                if len(parts) == 2:
                    return parts[0].strip(), parts[1].strip()
        
        # 如果没有找到分隔符，返回空
        return '', ''
    
    def _get_similar(self, shorten: bool, base: str, *args: str) -> int:
        """
        计算相似度
        
        这是从 JavaScript 翻译的匹配函数
        
        Parameters
        ----------
        shorten : bool
            是否使用子序列匹配（True）还是包含匹配（False）
        base : str
            基准字符串
        *args : str
            要比较的字符串
            
        Returns
        -------
        int
            相似度分数
        """
        # 将基准字符串根据非字母数字字符拆分
        base_elements = [elem for elem in re.split(r'[^a-zA-Z0-9]', base) if elem]
        
        should_return_zero = False
        total_included = 0
        
        for arg in args:
            # 将当前参数拆分
            arg_elements = [elem for elem in re.split(r'[^a-zA-Z0-9]', arg) if elem]
            
            # 计算当前参数中有多少元素被基准参数包含
            included_count = 0
            for elem in arg_elements:
                for base_elem in base_elements:
                    if len(base_elem) > 1 and len(elem) > 1:
                        if shorten:
                            # 子序列匹配
                            if self._is_subsequence(base_elem, elem) or self._is_subsequence(elem, base_elem):
                                included_count += 1
                                break
                        else:
                            # 包含匹配
                            if base_elem in elem or elem in base_elem:
                                included_count += 1
                                break
            
            # 如果任何一个参数与基准参数的被包含元素数量为0，则返回0
            if included_count == 0:
                should_return_zero = True
            
            total_included += included_count
        
        return 0 if should_return_zero else total_included
    
    def _is_subsequence(self, str1: str, subseq: str) -> bool:
        """
        检查 subseq 是否是 str1 的子序列
        
        Parameters
        ----------
        str1 : str
            主字符串
        subseq : str
            子序列
            
        Returns
        -------
        bool
            是否是子序列
        """
        j = 0  # subseq 的索引
        
        # 遍历 str1 的每个字符
        for i in range(len(str1)):
            if j < len(subseq) and str1[i] == subseq[j]:
                j += 1  # 当字符匹配时，移动 subseq 的索引
        
        # 如果 subseq 的所有字符都被找到，返回 True
        return j == len(subseq)


def test_matcher():
    """测试匹配器"""
    import logging
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # 测试数据
    orbitexch_events = [
        {
            'sport': 'Soccer',
            'competition': 'English Premier League',
            'event': 'Arsenal v Chelsea',
            'event_id': '1'
        },
        {
            'sport': 'Soccer',
            'competition': 'Spanish La Liga',
            'event': 'Real Madrid vs Barcelona',
            'event_id': '2'
        },
        {
            'sport': 'Basketball',
            'competition': 'NBA',
            'event': 'Lakers @ Warriors',
            'event_id': '3'
        }
    ]
    
    polymarket_events = [
        {
            'sport': 'Football',  # 不同叫法
            'competition': 'EPL',  # 缩写
            'event': 'Arsenal vs Chelsea FC',  # 略有不同
            'market_id': 'a'
        },
        {
            'sport': 'Soccer',
            'competition': 'La Liga',
            'event': 'Real Madrid v FC Barcelona',
            'market_id': 'b'
        },
        {
            'sport': 'Basketball',
            'competition': 'National Basketball Association',
            'event': 'LA Lakers vs Golden State Warriors',
            'market_id': 'c'
        },
        {
            'sport': 'Tennis',  # 不匹配
            'competition': 'ATP',
            'event': 'Federer v Nadal',
            'market_id': 'd'
        }
    ]
    
    # 执行匹配
    matcher = UniversalMatcher()
    matches = matcher.match_events(orbitexch_events, polymarket_events, "OrbitExch", "Polymarket")
    
    # 显示结果
    print('\n' + '=' * 70)
    print('匹配结果')
    print('=' * 70)
    
    for match in matches:
        print(f'\n{match}')
        print(f'  Event 1: {match.platform1_event["event"]}')
        print(f'  Event 2: {match.platform2_event["event"]}')
        print(f'  Sport Match: {match.sport_match}')
        print(f'  Competition Match: {match.competition_match}')


if __name__ == '__main__':
    test_matcher()
