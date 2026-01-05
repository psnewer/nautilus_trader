# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------------------------
#  Polymarket 市场发现 - 只获取活跃市场
# -------------------------------------------------------------------------------------------------

"""
Polymarket 市场发现 - 获取当前可交易的市场
"""

import logging
import json
from typing import List, Optional
from dataclasses import dataclass, asdict, field
from datetime import datetime
import re

from py_clob_client.client import ClobClient


@dataclass
class MarketEvent:
    """市场事件"""
    platform: str
    event_id: str
    sport: str
    competition: str
    event: str
    home_team: Optional[str] = None
    away_team: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    
    def to_dict(self):
        return asdict(self)


def extract_teams(event_name: str) -> tuple:
    """提取主队和客队"""
    clean = re.sub(r'^[A-Z]+:\s*', '', event_name)
    clean = re.sub(r'\s+\d{4}-\d{2}-\d{2}$', '', clean)
    clean = re.sub(r'\s+\(\d{2}/\d{2}/\d{4}\)$', '', clean)
    
    for pattern in [r'\s+vs\.?\s+', r'\s+v\.?\s+', r'\s+@\s+', r'\s+-\s+', r'\s+at\s+']:
        match = re.split(pattern, clean, flags=re.IGNORECASE)
        if len(match) == 2:
            return match[0].strip(), match[1].strip()
    
    return None, None


class PolymarketDiscovery:
    """Polymarket 市场发现"""
    
    def __init__(self):
        self._log = logging.getLogger('PolymarketDiscovery')
        self.client = ClobClient(host="https://clob.polymarket.com", chain_id=137)
    
    def discover_markets(self, sports_only: bool = True, active_only: bool = True) -> List[MarketEvent]:
        """
        发现市场
        
        Parameters
        ----------
        sports_only : bool
            只返回体育市场
        active_only : bool
            只返回活跃市场（closed=False）
        
        Returns
        -------
        List[MarketEvent]
            市场事件列表
        """
        self._log.info(f'发现 Polymarket 市场 (sports_only={sports_only}, active_only={active_only})...')
        
        # 获取所有市场
        response = self.client.get_markets()
        markets = response.get('data', [])
        
        self._log.info(f'获取到 {len(markets)} 个市场')
        
        # 过滤活跃市场 (closed=False)
        if active_only:
            original_count = len(markets)
            markets = [m for m in markets if m.get('closed') == False]
            self._log.info(f'活跃市场 (closed=False): {len(markets)} / {original_count}')
        
        # 过滤体育市场
        if sports_only:
            markets = self._filter_sports(markets)
            self._log.info(f'体育市场: {len(markets)}')
        
        # 转换格式
        events = []
        for market in markets:
            event = self._to_event(market)
            if event:
                events.append(event)
        
        self._log.info(f'✅ 发现 {len(events)} 个事件')
        return events
    
    def _filter_sports(self, markets: List[dict]) -> List[dict]:
        """过滤体育市场"""
        
        sports_keywords = [
            'nfl', 'nba', 'mlb', 'nhl', 'ncaab', 'ncaaf',
            'soccer', 'football', 'basketball', 'baseball', 'hockey',
            'tennis', 'golf', 'boxing', 'mma', 'ufc',
            'premier league', 'champions league', 'la liga',
        ]
        
        exclude_keywords = [
            'movie', 'film', 'election', 'presidential',
            'stock', 'bitcoin', 'crypto', 'award', 'oscar',
        ]
        
        result = []
        
        for m in markets:
            q = m.get('question', '').lower()
            
            # 排除
            if any(kw in q for kw in exclude_keywords):
                continue
            
            # vs 模式 + 体育关键词
            has_vs = bool(re.search(r'\s+vs\.?\s+|\s+v\.?\s+|\s+@\s+', q))
            has_sport = any(kw in q for kw in sports_keywords)
            
            # 运动前缀
            has_prefix = bool(re.match(r'^(NFL|NBA|MLB|NHL|NCAAB|NCAAF):', q, re.IGNORECASE))
            
            if has_prefix or (has_vs and has_sport):
                result.append(m)
        
        return result
    
    def _to_event(self, market: dict) -> Optional[MarketEvent]:
        """转换为事件格式"""
        
        question = market.get('question', '')
        if not question:
            return None
        
        home, away = extract_teams(question)
        sport = self._detect_sport(question)
        competition = self._detect_competition(question)
        
        return MarketEvent(
            platform='Polymarket',
            event_id=market.get('condition_id', ''),
            sport=sport,
            competition=competition,
            event=question,
            home_team=home,
            away_team=away,
            metadata={
                'volume': market.get('volume', 0),
                'active': market.get('active', False),
                'closed': market.get('closed', False),
                'end_date': market.get('end_date_iso'),
            }
        )
    
    def _detect_sport(self, question: str) -> str:
        """检测运动类型"""
        q = question.lower()
        
        patterns = {
            'Soccer': ['soccer', 'premier league', 'champions', 'la liga', 'bundesliga'],
            'Basketball': ['nba', 'basketball', 'ncaab'],
            'American Football': ['nfl', 'ncaaf', 'super bowl'],
            'Baseball': ['mlb', 'baseball'],
            'Ice Hockey': ['nhl', 'hockey'],
            'Tennis': ['tennis', 'atp', 'wta'],
            'Golf': ['golf', 'pga'],
            'Boxing': ['boxing'],
            'MMA': ['mma', 'ufc'],
        }
        
        for sport, keywords in patterns.items():
            if any(kw in q for kw in keywords):
                return sport
        
        return 'Other'
    
    def _detect_competition(self, question: str) -> str:
        """检测赛事"""
        q = question.lower()
        
        comps = {
            'English Premier League': ['premier league', 'epl'],
            'UEFA Champions League': ['champions league'],
            'La Liga': ['la liga'],
            'Bundesliga': ['bundesliga'],
            'NBA': ['nba'],
            'NFL': ['nfl'],
            'MLB': ['mlb'],
            'NHL': ['nhl'],
            'NCAA Basketball': ['ncaab'],
            'NCAA Football': ['ncaaf'],
        }
        
        for comp, keywords in comps.items():
            if any(kw in q for kw in keywords):
                return comp
        
        return ''


def main():
    """测试"""
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    print('=' * 70)
    print('Polymarket 市场发现 - 只获取活跃市场')
    print('=' * 70)
    print()
    
    discovery = PolymarketDiscovery()
    
    # 只获取活跃的体育市场
    events = discovery.discover_markets(sports_only=True, active_only=True)
    
    print(f'\n✅ 发现 {len(events)} 个活跃体育事件')
    
    if events:
        # 示例
        print('\n示例事件 (前 10 个):')
        for i, e in enumerate(events[:10], 1):
            status = "🟢" if not e.metadata.get('closed') else "🔴"
            print(f'\n{i}. {status} {e.event[:80]}')
            print(f'   Sport: {e.sport}')
            print(f'   Competition: {e.competition}')
            if e.home_team:
                print(f'   Teams: {e.home_team} vs {e.away_team}')
            print(f'   End Date: {e.metadata.get("end_date", "N/A")[:10]}')
        
        # 统计
        sports = {}
        for e in events:
            sports[e.sport] = sports.get(e.sport, 0) + 1
        
        print('\n运动类型分布:')
        for sport, count in sorted(sports.items(), key=lambda x: -x[1]):
            print(f'   {sport}: {count}')
        
        # 保存
        output = [e.to_dict() for e in events]
        with open('polymarket_events.json', 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        
        print(f'\n✅ 已保存到 polymarket_events.json')
    
    else:
        print('\n⚠️  没有找到活跃的体育市场')
        print('\n尝试查看所有市场（包括已关闭）:')
        
        # 获取所有市场看看
        all_events = discovery.discover_markets(sports_only=True, active_only=False)
        print(f'   所有体育市场（包括已关闭）: {len(all_events)}')
        
        if all_events:
            print('\n   示例（包括已关闭）:')
            for i, e in enumerate(all_events[:5], 1):
                status = "🟢" if not e.metadata.get('closed') else "🔴"
                print(f'   {i}. {status} {e.event[:60]}')


if __name__ == '__main__':
    main()
