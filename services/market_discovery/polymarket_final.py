# -*- coding: utf-8 -*-
"""Polymarket 市场发现 - 最终版本"""

import logging
import json
import requests
from typing import List, Optional, Dict, Tuple
from dataclasses import dataclass, asdict, field
from collections import Counter


@dataclass
class MarketEvent:
    """市场事件"""
    platform: str
    event_id: str
    sport: str
    competition: str
    event: str
    home_team: str
    away_team: str
    metadata: dict = field(default_factory=dict)
    
    def to_dict(self):
        return asdict(self)


class PolymarketFinal:
    """Polymarket 市场发现"""
    
    def __init__(self):
        self._log = logging.getLogger('PolymarketFinal')
        self.gamma_api = 'https://gamma-api.polymarket.com'
        self.session = requests.Session()
    
    def discover_markets(self, active_only: bool = True) -> List[MarketEvent]:
        """发现市场"""
        self._log.info('开始发现市场...')
        
        sports_config = self._get_sports_config()
        if not sports_config:
            return []
        
        self._log.info(f'获取到 {len(sports_config)} 个运动配置')
        
        tag_counter = self._count_tags(sports_config)
        sport_tags = self._select_tags(sports_config, tag_counter)
        
        all_events = []
        for sport_key, (t1, t2) in sport_tags.items():
            events = self._get_events(t1, t2, not active_only)
            all_events.extend(events)
            self._log.info(f'{sport_key}: {len(events)} 个事件')
        
        self._log.info(f'总计: {len(all_events)} 个事件')
        return all_events
    
    def _get_sports_config(self) -> List[Dict]:
        """获取 sports 配置"""
        try:
            response = self.session.get(f'{self.gamma_api}/sports', timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self._log.error(f'获取失败: {e}')
            return []
    
    def _count_tags(self, sports_config: List[Dict]) -> Counter:
        """统计 tag 重复次数"""
        tag_counter = Counter()
        for sport in sports_config:
            tags_str = sport.get('tags', '')
            if tags_str:
                tag_counter.update(tags_str.split(','))
        return tag_counter
    
    def _select_tags(self, sports_config: List[Dict], tag_counter: Counter) -> Dict[str, Tuple[str, str]]:
        """选择 t1 和 t2"""
        result = {}
        
        for sport in sports_config:
            sport_key = sport.get('sport', '')
            tags_str = sport.get('tags', '')
            
            if not sport_key or not tags_str:
                continue
            
            tag_list = tags_str.split(',')
            
            min_count = min(tag_counter[t] for t in tag_list)
            t1_candidates = [t for t in tag_list if tag_counter[t] == min_count]
            t1 = t1_candidates[-1]
            
            counts = sorted(set(tag_counter[t] for t in tag_list))
            if len(counts) >= 2:
                second_min_count = counts[1]
                t2_candidates = [t for t in tag_list if tag_counter[t] == second_min_count]
                t2 = t2_candidates[-1]
            else:
                t2 = t1
            
            result[sport_key] = (t1, t2)
        
        return result
    
    def _get_events(self, t1: str, t2: str, closed: bool) -> List[MarketEvent]:
        """获取事件"""
        try:
            params = {'tag_id': t1, 'closed': str(closed).lower()}
            response = self.session.get(f'{self.gamma_api}/events', params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            events = []
            for item in data:
                event = self._parse_event(item, t1, t2)
                if event:
                    events.append(event)
            
            return events
        except Exception as e:
            self._log.error(f'获取事件失败: {e}')
            return []
    
    def _parse_event(self, data: Dict, t1: str, t2: str) -> Optional[MarketEvent]:
        """解析事件
        
        注意: 
        - id=t1 的 label -> competition
        - id=t2 的 label -> sport
        """
        home_team = data.get('homeTeamName', '')
        away_team = data.get('awayTeamName', '')
        
        if not home_team or not away_team:
            return None
        
        tags = data.get('tags', [])
        competition = self._find_label(tags, t1) or 'Unknown'
        sport = self._find_label(tags, t2) or 'Unknown'
        
        return MarketEvent(
            platform='Polymarket',
            event_id=str(data.get('id', '')),
            sport=sport,
            competition=competition,
            event=f'{home_team} vs {away_team}',
            home_team=home_team,
            away_team=away_team,
            metadata={'start_date': data.get('startDate'), 'closed': data.get('closed', False)}
        )
    
    def _find_label(self, tags: List[Dict], tag_id: str) -> str:
        """从 tags 找 label"""
        for tag in tags:
            if str(tag.get('id', '')) == str(tag_id):
                return tag.get('label', '')
        return ''


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
    
    print('=' * 70)
    print('Polymarket 市场发现')
    print('=' * 70)
    
    client = PolymarketFinal()
    events = client.discover_markets(active_only=True)
    
    print(f'\n发现 {len(events)} 个活跃事件')
    
    if events:
        print('\n示例 (前 10 个):')
        for i, e in enumerate(events[:10], 1):
            print(f'\n{i}. {e.event}')
            print(f'   Sport: {e.sport} | Competition: {e.competition}')
        
        sports = {}
        for e in events:
            sports[e.sport] = sports.get(e.sport, 0) + 1
        
        print('\n运动类型分布:')
        for sport, count in sorted(sports.items(), key=lambda x: -x[1]):
            print(f'   {sport}: {count}')
        
        with open('polymarket_events.json', 'w', encoding='utf-8') as f:
            json.dump([e.to_dict() for e in events], f, indent=2, ensure_ascii=False)
        
        print('\n已保存到 polymarket_events.json')


if __name__ == '__main__':
    main()
