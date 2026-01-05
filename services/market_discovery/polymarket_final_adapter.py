# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------------------------
#  Polymarket 适配器 - 最终版
# -------------------------------------------------------------------------------------------------

"""
Polymarket 市场发现适配器（支持已关闭市场）
"""

import logging
from typing import List, Optional
from dataclasses import dataclass, asdict, field
from datetime import datetime
import re

from py_clob_client.client import ClobClient


@dataclass
class StandardEvent:
    """标准化的事件数据"""
    platform: str
    event_id: str
    event_name: str
    sport: str
    competition: str
    home_team: Optional[str] = None
    away_team: Optional[str] = None
    participants: List[str] = field(default_factory=list)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    is_live: bool = False
    is_active: bool = True
    discovered_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict = field(default_factory=dict)
    
    def to_dict(self):
        return asdict(self)


def extract_teams_from_name(event_name: str):
    """从事件名称提取球队"""
    clean_name = re.sub(r'^[A-Z]+:\s*', '', event_name)
    clean_name = re.sub(r'\s+\d{4}-\d{2}-\d{2}$', '', clean_name)
    
    separators = [
        r'\s+vs\.?\s+',
        r'\s+v\.?\s+',
        r'\s+@\s+',
        r'\s+-\s+',
        r'\s+at\s+',
    ]
    
    for sep_pattern in separators:
        match = re.split(sep_pattern, clean_name, flags=re.IGNORECASE)
        if len(match) == 2:
            return match[0].strip(), match[1].strip()
    
    return None, None


class PolymarketFinalAdapter:
    """Polymarket 最终适配器"""
    
    def __init__(self):
        self._log = logging.getLogger('PolymarketFinal')
        
        self.client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
        )
    
    def discover_markets(
        self,
        limit: int = 200,
        sports_only: bool = True,
        active_only: bool = False  # 默认包含已关闭市场
    ) -> List[StandardEvent]:
        """
        发现市场
        
        Parameters
        ----------
        limit : int
            最大返回数量
        sports_only : bool
            是否只返回体育市场
        active_only : bool
            是否只返回活跃市场（False 包含已关闭市场）
        """
        
        self._log.info(f'发现 Polymarket 市场 (limit={limit}, active_only={active_only})...')
        
        # 获取市场
        response = self.client.get_markets()
        markets = response.get('data', [])
        
        self._log.info(f'获取到 {len(markets)} 个市场')
        
        # 过滤活跃市场
        if active_only:
            markets = [m for m in markets if m.get('active', False) and not m.get('closed', False)]
            self._log.info(f'活跃市场: {len(markets)}')
        
        # 限制数量
        markets = markets[:limit]
        
        # 过滤体育市场
        if sports_only:
            markets = self._filter_sports_markets(markets)
            self._log.info(f'体育市场: {len(markets)}')
        
        # 转换为标准格式
        events = []
        for market in markets:
            event = self._convert_to_standard_event(market)
            if event:
                events.append(event)
        
        self._log.info(f'✅ 转换为 {len(events)} 个标准事件')
        return events
    
    def _filter_sports_markets(self, markets: List[dict]) -> List[dict]:
        """过滤体育市场"""
        
        sports_prefixes = ['nfl:', 'nba:', 'mlb:', 'nhl:', 'ncaab:', 'ncaaf:']
        
        sports_keywords = [
            'nfl', 'nba', 'mlb', 'nhl', 'soccer', 'football', 'basketball', 
            'baseball', 'hockey', 'tennis', 'golf', 'boxing', 'mma', 'ufc',
            'ncaab', 'ncaaf', 'premier league', 'champions league',
        ]
        
        exclude_keywords = [
            'movie', 'film', 'box office', 'grossing',
            'election', 'presidential', 'senate', 'congress',
            'stock', 'bitcoin', 'crypto', 'price',
            'award', 'oscar', 'grammy',
        ]
        
        sports_markets = []
        
        for market in markets:
            question = market.get('question', '').lower()
            description = market.get('description', '').lower()
            
            # 排除
            if any(kw in question for kw in exclude_keywords):
                continue
            
            # 强包含
            if any(question.startswith(prefix) for prefix in sports_prefixes):
                sports_markets.append(market)
                continue
            
            # vs 模式 + 体育关键词
            has_vs = bool(re.search(r'\s+vs\.?\s+|\s+v\.?\s+|\s+@\s+', question))
            has_sport_kw = any(kw in question or kw in description for kw in sports_keywords)
            
            if has_vs and has_sport_kw:
                sports_markets.append(market)
        
        return sports_markets
    
    def _convert_to_standard_event(self, market: dict) -> Optional[StandardEvent]:
        """转换为标准事件格式"""
        
        question = market.get('question', '')
        if not question:
            return None
        
        home_team, away_team = extract_teams_from_name(question)
        sport = self._detect_sport(question)
        competition = self._detect_competition(question, market.get('description', ''))
        
        event = StandardEvent(
            platform='Polymarket',
            event_id=market.get('condition_id', ''),
            event_name=question,
            sport=sport,
            competition=competition,
            home_team=home_team,
            away_team=away_team,
            participants=[],
            start_time=market.get('end_date_iso'),
            end_time=market.get('end_date_iso'),
            is_live=False,
            is_active=market.get('active', False) and not market.get('closed', False),
            metadata={
                'question': question,
                'description': market.get('description', ''),
                'volume': market.get('volume', 0),
                'closed': market.get('closed', False),
            }
        )
        
        return event
    
    def _detect_sport(self, question: str) -> str:
        """检测运动类型"""
        
        question_lower = question.lower()
        
        sport_patterns = {
            'Soccer': ['soccer', 'premier league', 'champions league', 'la liga', 'bundesliga'],
            'Basketball': ['nba', 'basketball', 'ncaab'],
            'American Football': ['nfl', 'ncaaf', 'super bowl', 'football'],
            'Baseball': ['mlb', 'baseball'],
            'Baseball': ['mlb', 'baseball'],
            'Ice Hockey': ['nhl', 'hockey'],
            'Tennis': ['tennis', 'atp', 'wta'],
            'Golf': ['golf', 'pga'],
            'Boxing': ['boxing'],
            'MMA': ['mma', 'ufc'],
        }
        
        for sport, keywords in sport_patterns.items():
            if any(kw in question_lower for kw in keywords):
                return sport
        
        return 'Other'
    
    def _detect_competition(self, question: str, description: str) -> str:
        """检测赛事名称"""
        
        combined = (question + ' ' + description).lower()
        
        competitions = {
            'English Premier League': ['premier league', 'epl'],
            'UEFA Champions League': ['champions league', 'ucl'],
            'La Liga': ['la liga'],
            'Bundesliga': ['bundesliga'],
            'NBA': ['nba'],
            'NFL': ['nfl'],
            'MLB': ['mlb'],
            'NHL': ['nhl'],
            'NCAA Basketball': ['ncaab'],
            'NCAA Football': ['ncaaf'],
        }
        
        for comp_name, keywords in competitions.items():
            if any(kw in combined for kw in keywords):
                return comp_name
        
        return ''


def test_adapter():
    """测试适配器"""
    import json
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print('=' * 70)
    print('测试 Polymarket 最终适配器')
    print('=' * 70)
    print()
    
    adapter = PolymarketFinalAdapter()
    
    # 获取所有体育市场（包括已关闭的）
    events = adapter.discover_markets(
        limit=500,
        sports_only=True,
        active_only=False  # 包含已关闭市场
    )
    
    print(f'\n✅ 获取到 {len(events)} 个体育事件（包括已关闭）')
    
    if events:
        # 统计活跃vs已关闭
        active_count = sum(1 for e in events if e.is_active)
        closed_count = len(events) - active_count
        
        print(f'   活跃: {active_count}')
        print(f'   已关闭: {closed_count}')
        
        print('\n示例事件:')
        for i, event in enumerate(events[:10], 1):
            status = "🟢" if event.is_active else "🔴"
            print(f'\n{i}. {status} {event.event_name[:80]}')
            print(f'   Sport: {event.sport} | Competition: {event.competition}')
            if event.home_team:
                print(f'   Home: {event.home_team}')
                print(f'   Away: {event.away_team}')
        
        # 保存
        with open('polymarket_events.json', 'w', encoding='utf-8') as f:
            json.dump([e.to_dict() for e in events], f, indent=2, ensure_ascii=False)
        
        print(f'\n✅ 已保存到 polymarket_events.json')
    
    # 统计
    sports = {}
    for event in events:
        sports[event.sport] = sports.get(event.sport, 0) + 1
    
    if sports:
        print('\n运动类型分布:')
        for sport, count in sorted(sports.items(), key=lambda x: -x[1]):
            print(f'   {sport}: {count}')


if __name__ == '__main__':
    test_adapter()
