# -*- coding: utf-8 -*-
"""
Polymarket 市场发现 - 完整实现

逻辑:
1. 爬取 https://polymarket.com/sports 获取 sport 和 competition 映射
2. 从 /sports API 获取配置，为每个 sport 选择最少重复的 tag
3. 用 tag_id 从 /events 获取事件
4. 从事件的 tags 中匹配 competition，从而确定 sport
5. 从 title 提取主队和客队
"""

import logging
import json
import requests
from typing import List, Optional, Dict, Set
from dataclasses import dataclass, asdict, field
from collections import Counter
from bs4 import BeautifulSoup
import re


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


class PolymarketComplete:
    """Polymarket 完整实现"""
    
    def __init__(self):
        self._log = logging.getLogger('PolymarketComplete')
        self.gamma_api = 'https://gamma-api.polymarket.com'
        self.session = requests.Session()
        
        # sport -> [competitions]
        self.sport_competitions: Dict[str, List[str]] = {}
        
        # competition -> sport
        self.competition_to_sport: Dict[str, str] = {}
    
    def discover_markets(self, active_only: bool = True) -> List[MarketEvent]:
        """发现市场"""
        
        self._log.info('开始发现市场...')
        
        # 步骤 1: 爬取网页获取 sport 和 competition 映射
        self._scrape_sports_page()
        
        if not self.sport_competitions:
            self._log.warning('未能从网页获取 sport/competition 映射，使用 API fallback')
            return self._fallback_discovery(active_only)
        
        # 步骤 2: 从 API 获取配置
        sports_config = self._get_sports_config()
        if not sports_config:
            return []
        
        # 步骤 3: 为每个 sport 选择最少重复的 tag
        tag_counter = self._count_tags(sports_config)
        sport_tags = self._select_unique_tags(sports_config, tag_counter)
        
        # 步骤 4: 获取事件
        all_events = []
        for sport_key, tag_id in sport_tags.items():
            events = self._get_events(tag_id, not active_only)
            all_events.extend(events)
            self._log.info(f'{sport_key}: {len(events)} 个事件')
        
        self._log.info(f'总计: {len(all_events)} 个事件')
        return all_events
    
    def _scrape_sports_page(self):
        """爬取 sports 页面获取 sport 和 competition"""
        
        try:
            self._log.info('爬取 https://polymarket.com/sports ...')
            
            response = self.session.get('https://polymarket.com/sports', timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 找所有 class="group/sports-item" 的 div
            sport_items = soup.find_all('div', class_='group/sports-item')
            
            self._log.info(f'找到 {len(sport_items)} 个运动项')
            
            for item in sport_items:
                # 获取 sport 名称（可能在属性或文本中）
                sport_name = self._extract_sport_name(item)
                
                if not sport_name:
                    continue
                
                # 找该 sport 下的所有 competition (class="block" 的 a 标签)
                competitions = []
                
                # 可能需要点击展开，这里先尝试直接查找
                comp_links = item.find_all('a', class_='block')
                
                for link in comp_links:
                    comp_name = link.get_text(strip=True)
                    if comp_name:
                        competitions.append(comp_name)
                        self.competition_to_sport[comp_name] = sport_name
                
                if competitions:
                    self.sport_competitions[sport_name] = competitions
                    self._log.info(f'{sport_name}: {len(competitions)} 个 competition')
        
        except Exception as e:
            self._log.error(f'爬取失败: {e}')
    
    def _extract_sport_name(self, item) -> str:
        """从 sport item 提取 sport 名称"""
        
        # 尝试多种方式提取
        # 1. 从 data 属性
        for attr in ['data-sport', 'data-name', 'data-value']:
            if item.has_attr(attr):
                return item[attr]
        
        # 2. 从文本
        text = item.get_text(strip=True)
        if text:
            # 可能是第一行或标题
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            if lines:
                return lines[0]
        
        return ''
    
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
        """为每个 sport 选择最少重复的 tag"""
        
        result = {}
        
        for sport in sports_config:
            sport_key = sport.get('sport', '')
            tags_str = sport.get('tags', '')
            
            if not sport_key or not tags_str:
                continue
            
            tag_list = tags_str.split(',')
            
            # 从左向右遍历，找重复最少的
            min_count = float('inf')
            selected_tag = tag_list[0]
            
            for tag in tag_list:
                if tag_counter[tag] < min_count:
                    min_count = tag_counter[tag]
                    selected_tag = tag
            
            result[sport_key] = selected_tag
        
        return result
    
    def _get_events(self, tag_id: str, closed: bool) -> List[MarketEvent]:
        """获取事件"""
        
        try:
            params = {'tag_id': tag_id, 'closed': str(closed).lower()}
            response = self.session.get(f'{self.gamma_api}/events', params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            events = []
            for item in data:
                event = self._parse_event(item)
                if event:
                    events.append(event)
            
            return events
        
        except Exception as e:
            self._log.error(f'获取事件失败: {e}')
            return []
    
    def _parse_event(self, data: Dict) -> Optional[MarketEvent]:
        """解析事件"""
        
        # 从 title 提取主队和客队
        title = data.get('title', '')
        home_team, away_team = self._extract_teams(title)
        
        if not home_team or not away_team:
            return None
        
        # 从 tags 匹配 competition 和 sport
        tags = data.get('tags', [])
        competition, sport = self._match_competition_sport(tags)
        
        if not competition:
            competition = 'Unknown'
        if not sport:
            sport = 'Unknown'
        
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
            }
        )
    
    def _extract_teams(self, title: str) -> tuple:
        """从 title 提取主队和客队"""
        
        # 尝试用 "vs." 或 "vs" 拆分
        for separator in [' vs. ', ' vs ', ' v. ', ' v ']:
            if separator in title:
                parts = title.split(separator, 1)
                if len(parts) == 2:
                    return parts[0].strip(), parts[1].strip()
        
        return None, None
    
    def _match_competition_sport(self, tags: List[Dict]) -> tuple:
        """从 tags 匹配 competition 和 sport"""
        
        # 遍历 tags，找到和 competition 相同的 label
        for tag in tags:
            label = tag.get('label', '')
            
            if label in self.competition_to_sport:
                competition = label
                sport = self.competition_to_sport[label]
                return competition, sport
        
        return None, None
    
    def _fallback_discovery(self, active_only: bool) -> List[MarketEvent]:
        """Fallback: 直接用 API，不依赖网页爬取"""
        
        self._log.info('使用 API fallback 模式')
        
        sports_config = self._get_sports_config()
        if not sports_config:
            return []
        
        tag_counter = self._count_tags(sports_config)
        sport_tags = self._select_unique_tags(sports_config, tag_counter)
        
        all_events = []
        
        for sport_key, tag_id in sport_tags.items():
            events = self._get_events_fallback(tag_id, sport_key, not active_only)
            all_events.extend(events)
        
        return all_events
    
    def _get_events_fallback(self, tag_id: str, sport_key: str, closed: bool) -> List[MarketEvent]:
        """Fallback 获取事件"""
        
        try:
            params = {'tag_id': tag_id, 'closed': str(closed).lower()}
            response = self.session.get(f'{self.gamma_api}/events', params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            events = []
            
            for item in data:
                title = item.get('title', '')
                home, away = self._extract_teams(title)
                
                if not home or not away:
                    continue
                
                event = MarketEvent(
                    platform='Polymarket',
                    event_id=str(item.get('id', '')),
                    sport=sport_key.title(),
                    competition='Unknown',
                    event=f'{home} vs {away}',
                    home_team=home,
                    away_team=away,
                    metadata={'title': title}
                )
                
                events.append(event)
            
            return events
        
        except Exception as e:
            self._log.error(f'Fallback 获取失败: {e}')
            return []


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
    
    print('=' * 70)
    print('Polymarket 市场发现 - 完整实现')
    print('=' * 70)
    
    client = PolymarketComplete()
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
