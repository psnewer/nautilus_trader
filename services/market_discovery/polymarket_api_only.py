# -*- coding: utf-8 -*-
"""
Polymarket 市场发现 - 纯 API 方式

不依赖网页爬虫，只用 API
"""

import logging
import json
import requests
from typing import List, Optional, Dict
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


class PolymarketAPIOnly:
    """Polymarket 纯 API 实现"""
    
    def __init__(self):
        self._log = logging.getLogger('PolymarketAPI')
        self.gamma_api = 'https://gamma-api.polymarket.com'
        self.session = requests.Session()
    
    def discover_markets(self, active_only: bool = True) -> List[MarketEvent]:
        """发现市场"""
        
        self._log.info('开始发现市场...')
        
        # 获取 sports 配置
        sports_config = self._get_sports_config()
        if not sports_config:
            return []
        
        self._log.info(f'获取到 {len(sports_config)} 个运动配置')
        
        # 统计 tag
        tag_counter = self._count_tags(sports_config)
        
        # 为每个 sport 选择最少重复的 tag
        sport_tags = self._select_unique_tags(sports_config, tag_counter)
        
        # 获取事件
        all_events = []
        
        for sport_key, tag_id in sport_tags.items():
            self._log.info(f'获取 {sport_key} (tag={tag_id})...')
            
            events = self._get_events(tag_id, sport_key, not active_only)
            all_events.extend(events)
            
            self._log.info(f'  {sport_key}: {len(events)} 个事件')
        
        self._log.info(f'✅ 总计: {len(all_events)} 个事件')
        return all_events
    
    def _get_sports_config(self) -> List[Dict]:
        """获取 sports 配置"""
        try:
            response = self.session.get(f'{self.gamma_api}/sports', timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self._log.error(f'获取配置失败: {e}')
            return []
    
    def _count_tags(self, sports_config: List[Dict]) -> Counter:
        """统计 tag 重复次数"""
        tag_counter = Counter()
        for sport in sports_config:
            tags_str = sport.get('tags', '')
            if tags_str:
                tag_counter.update(tags_str.split(','))
        return tag_counter
    
    def _select_unique_tags(self, sports_config: List[Dict], tag_counter: Counter) -> Dict[str, str]:
        """选择最少重复的 tag"""
        
        result = {}
        
        for sport in sports_config:
            sport_key = sport.get('sport', '')
            tags_str = sport.get('tags', '')
            
            if not sport_key or not tags_str:
                continue
            
            tag_list = tags_str.split(',')
            
            # 从左向右找重复最少的
            min_count = float('inf')
            selected_tag = tag_list[0]
            
            for tag in tag_list:
                if tag_counter[tag] < min_count:
                    min_count = tag_counter[tag]
                    selected_tag = tag
            
            result[sport_key] = selected_tag
            
            self._log.debug(f'{sport_key}: 选择 tag={selected_tag} (count={tag_counter[selected_tag]})')
        
        return result
    
    def _get_events(self, tag_id: str, sport_key: str, closed: bool) -> List[MarketEvent]:
        """获取事件"""
        
        try:
            params = {'tag_id': tag_id, 'closed': str(closed).lower()}
            response = self.session.get(f'{self.gamma_api}/events', params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            if not isinstance(data, list):
                self._log.warning(f'期望列表，得到: {type(data)}')
                return []
            
            events = []
            
            for item in data:
                event = self._parse_event(item, sport_key, tag_id)
                if event:
                    events.append(event)
            
            return events
        
        except Exception as e:
            self._log.error(f'获取事件失败: {e}')
            return []
    
    def _parse_event(self, data: Dict, sport_key: str, tag_id: str) -> Optional[MarketEvent]:
        """
        解析事件
        
        从 tags 中找到 id=tag_id 的 label 作为 competition
        sport 就用 sport_key
        """
        
        # 从 title 提取主客队
        title = data.get('title', '')
        home_team, away_team = self._extract_teams(title)
        
        if not home_team or not away_team:
            return None
        
        # 从 tags 找 competition
        tags = data.get('tags', [])
        competition = self._find_competition(tags, tag_id)
        
        if not competition:
            competition = 'Unknown'
        
        # 映射 sport 名称
        sport = self._map_sport_name(sport_key)
        
        return MarketEvent(
            platform='Polymarket',
            event_id=str(data.get('id', '')),
            sport=sport,
            competition=competition,
            event=f'{home_team} vs {away_team}',
            home_team=home_team,
            away_team=away_team,
            metadata={
                'title': title,
                'start_date': data.get('startDate'),
                'closed': data.get('closed', False),
                'sport_key': sport_key,
            }
        )
    
    def _extract_teams(self, title: str) -> tuple:
        """从 title 提取主客队"""
        
        for separator in [' vs. ', ' vs ', ' v. ', ' v ']:
            if separator in title:
                parts = title.split(separator, 1)
                if len(parts) == 2:
                    return parts[0].strip(), parts[1].strip()
        
        return None, None
    
    def _find_competition(self, tags: List[Dict], tag_id: str) -> str:
        """从 tags 找到 id=tag_id 的 label"""
        
        for tag in tags:
            if str(tag.get('id', '')) == str(tag_id):
                return tag.get('label', '')
        
        return ''
    
    def _map_sport_name(self, sport_key: str) -> str:
        """映射 sport 名称"""
        
        mapping = {
            'nfl': 'American Football',
            'nba': 'Basketball',
            'ncaab': 'Basketball',
            'ncaaf': 'American Football',
            'mlb': 'Baseball',
            'nhl': 'Ice Hockey',
            'epl': 'Soccer',
            'ucl': 'Soccer',
            'laliga': 'Soccer',
            'bundesliga': 'Soccer',
            'seriea': 'Soccer',
            'ligue1': 'Soccer',
            'mls': 'Soccer',
            'tennis': 'Tennis',
            'golf': 'Golf',
            'mma': 'MMA',
            'boxing': 'Boxing',
        }
        
        return mapping.get(sport_key.lower(), sport_key.title())


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
    
    print('=' * 70)
    print('Polymarket 市场发现 - 纯 API')
    print('=' * 70)
    
    client = PolymarketAPIOnly()
    events = client.discover_markets(active_only=True)
    
    print(f'\n✅ 发现 {len(events)} 个活跃事件')
    
    if events:
        print('\n示例 (前 15 个):')
        for i, e in enumerate(events[:15], 1):
            print(f'\n{i}. {e.event}')
            print(f'   Sport: {e.sport} | Competition: {e.competition}')
        
        # 统计
        sports = {}
        for e in events:
            sports[e.sport] = sports.get(e.sport, 0) + 1
        
        print('\n运动类型分布:')
        for sport, count in sorted(sports.items(), key=lambda x: -x[1]):
            print(f'   {sport}: {count}')
        
        # 按 competition 统计
        comps = {}
        for e in events:
            comp = e.competition
            comps[comp] = comps.get(comp, 0) + 1
        
        print('\nCompetition 分布 (前 15):')
        for comp, count in sorted(comps.items(), key=lambda x: -x[1])[:15]:
            print(f'   {comp}: {count}')
        
        # 保存
        with open('polymarket_events.json', 'w', encoding='utf-8') as f:
            json.dump([e.to_dict() for e in events], f, indent=2, ensure_ascii=False)
        
        print('\n✅ 已保存到 polymarket_events.json')


if __name__ == '__main__':
    main()
