# -*- coding: utf-8 -*-
"""
Polymarket 市场发现
"""

import logging
import json
import requests
from typing import List, Optional, Dict
from dataclasses import dataclass, asdict, field
from collections import Counter
from bs4 import BeautifulSoup


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


class PolymarketScraper:
    """Polymarket 爬虫"""
    
    def __init__(self):
        self._log = logging.getLogger('PolymarketScraper')
        self.gamma_api = 'https://gamma-api.polymarket.com'
        self.session = requests.Session()
        self.competition_map: Dict[str, tuple] = {}
    
    def discover_markets(self, active_only: bool = True) -> List[MarketEvent]:
        """发现市场"""
        
        self._log.info('开始发现市场...')
        
        # 爬取网页
        success = self._scrape_sports_page()
        
        if not success or not self.competition_map:
            self._log.warning('网页爬取失败')
            return []
        
        self._log.info(f'获取到 {len(self.competition_map)} 个 competition')
        
        # 获取 API 配置
        sports_config = self._get_sports_config()
        if not sports_config:
            return []
        
        # 选择 tag
        tag_counter = self._count_tags(sports_config)
        sport_tags = self._select_unique_tags(sports_config, tag_counter)
        
        # 获取事件
        all_events = []
        
        for sport_key, tag_id in sport_tags.items():
            events = self._get_events(tag_id, not active_only)
            all_events.extend(events)
            self._log.info(f'{sport_key}: {len(events)} 个事件')
        
        self._log.info(f'总计: {len(all_events)} 个事件')
        return all_events
    
    def _scrape_sports_page(self) -> bool:
        """爬取 sports 页面"""
        
        try:
            self._log.info('爬取 https://polymarket.com/sports')
            
            response = self.session.get('https://polymarket.com/sports', timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 找所有 group/sports-item
            sport_items = soup.find_all('div', class_='group/sports-item')
            
            self._log.info(f'找到 {len(sport_items)} 个运动')
            
            for item in sport_items:
                # 获取该 item 下的所有直接子 div
                child_divs = item.find_all('div', recursive=False)
                
                if len(child_divs) < 2:
                    continue
                
                # 第一个 div: sport
                first_div = child_divs[0]
                sport_p = first_div.find('p')
                
                if not sport_p:
                    continue
                
                sport_name = sport_p.get_text(strip=True)
                
                # 第二个 div: competitions
                second_div = child_divs[1]
                
                # 找所有 <a class="block">
                comp_links = second_div.find_all('a', class_='block')
                
                for link in comp_links:
                    comp_p = link.find('p')
                    
                    if comp_p:
                        comp_name = comp_p.get_text(strip=True)
                        
                        # 存储映射 (小写键)
                        self.competition_map[comp_name.lower()] = (comp_name, sport_name)
                
                self._log.info(f'{sport_name}: {len(comp_links)} competitions')
            
            return True
        
        except Exception as e:
            self._log.error(f'爬取失败: {e}')
            import traceback
            traceback.print_exc()
            return False
    
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
        """统计 tag"""
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
            self._log.error(f'获取失败: {e}')
            return []
    
    def _parse_event(self, data: Dict) -> Optional[MarketEvent]:
        """解析事件"""
        
        title = data.get('title', '')
        
        # 过滤 "More Markets"
        if 'More Markets' in title:
            return None
        
        home_team, away_team = self._extract_teams(title)
        
        if not home_team or not away_team:
            return None
        
        tags = data.get('tags', [])
        competition, sport = self._match_competition_sport(tags)
        
        return MarketEvent(
            platform='Polymarket',
            event_id=str(data.get('id', '')),
            sport=sport or 'Unknown',
            competition=competition or 'Unknown',
            event=f'{home_team} vs {away_team}',
            home_team=home_team,
            away_team=away_team,
            metadata={'title': title, 'start_date': data.get('startDate')}
        )
    
    def _extract_teams(self, title: str) -> tuple:
        """从 title 提取主客队"""
        for sep in [' vs. ', ' vs ', ' v. ', ' v ']:
            if sep in title:
                parts = title.split(sep, 1)
                if len(parts) == 2:
                    return parts[0].strip(), parts[1].strip()
        return None, None
    
    def _match_competition_sport(self, tags: List[Dict]) -> tuple:
        """匹配 competition 和 sport"""
        for tag in tags:
            label = tag.get('label', '')
            label_lower = label.lower()
            
            if label_lower in self.competition_map:
                return self.competition_map[label_lower]
        
        return None, None


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
    
    print('=' * 70)
    print('Polymarket 市场发现')
    print('=' * 70)
    
    client = PolymarketScraper()
    events = client.discover_markets(active_only=True)
    
    print(f'\n发现 {len(events)} 个事件')
    
    if events:
        print('\n示例 (前 10 个):')
        for i, e in enumerate(events[:10], 1):
            print(f'{i}. {e.event}')
            print(f'   {e.sport} | {e.competition}')
        
        sports = {}
        for e in events:
            sports[e.sport] = sports.get(e.sport, 0) + 1
        
        print('\n运动分布:')
        for sport, count in sorted(sports.items(), key=lambda x: -x[1]):
            print(f'   {sport}: {count}')
        
        with open('polymarket_events.json', 'w') as f:
            json.dump([e.to_dict() for e in events], f, indent=2, ensure_ascii=False)
        
        print('\n保存到 polymarket_events.json')


if __name__ == '__main__':
    main()
