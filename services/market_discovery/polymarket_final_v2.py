# -*- coding: utf-8 -*-
"""
Polymarket 市场发现 - V2
"""

import logging
import json
import requests
from typing import List, Optional, Dict
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


class PolymarketV2:
    """Polymarket V2"""
    
    def __init__(self):
        self._log = logging.getLogger('Polymarket')
        self.gamma_api = 'https://gamma-api.polymarket.com'
        self.session = requests.Session()
        
        # 网页映射: competition -> sport
        self.web_competition_to_sport: Dict[str, str] = {}
    
    def discover_markets(self, active_only: bool = True) -> List[MarketEvent]:
        """发现市场"""
        
        self._log.info('开始 Polymarket 市场发现')
        
        # 步骤 1: 爬取网页
        self._log.info('步骤 1: 爬取网页')
        self._scrape_sports_page()
        self._log.info(f'获取 {len(self.web_competition_to_sport)} 个映射')
        
        # 步骤 2: 获取 API 配置
        self._log.info('步骤 2: 获取 API 配置')
        sports_config = self._get_sports_config()
        if not sports_config:
            return []
        
        # 步骤 3: 选择 tag (重复次数 <= 2)
        self._log.info('步骤 3: 选择 tag (重复 <= 2)')
        tag_counter = self._count_tags(sports_config)
        sport_tags_list = self._select_tags_v2(sports_config, tag_counter)
        
        # 步骤 4: 获取事件
        self._log.info('步骤 4: 获取事件')
        all_events = []
        
        for sport_key, tags in sport_tags_list.items():
            # 从小到大遍历 tag
            for tag_id in sorted(tags, key=int):
                events = self._get_events(tag_id, active_only)
                
                if events:
                    # 找到事件，停止遍历该 sport 的 tag
                    all_events.extend(events)
                    self._log.info(f'{sport_key} (tag={tag_id}): {len(events)} 个事件 ✅')
                    break
                else:
                    self._log.info(f'{sport_key} (tag={tag_id}): 0 个事件, 继续...')
            else:
                self._log.info(f'{sport_key}: 所有 tag 都没有事件 ❌')
        
        self._log.info(f'总计: {len(all_events)} 个事件')
        return all_events
    
    def _scrape_sports_page(self):
        """爬取网页"""
        try:
            response = self.session.get('https://polymarket.com/sports', timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            sport_items = soup.find_all('div', class_='group/sports-item')
            
            for item in sport_items:
                child_divs = item.find_all('div', recursive=False)
                
                if len(child_divs) < 2:
                    continue
                
                sport_p = child_divs[0].find('p')
                if not sport_p:
                    continue
                
                sport_name = sport_p.get_text(strip=True)
                comp_links = child_divs[1].find_all('a', class_='block')
                
                for link in comp_links:
                    comp_p = link.find('p')
                    if comp_p:
                        comp_name = comp_p.get_text(strip=True)
                        self.web_competition_to_sport[comp_name] = sport_name
        
        except Exception as e:
            self._log.error(f'爬取失败: {e}')
    
    def _get_sports_config(self) -> List[Dict]:
        """获取 API 配置"""
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
    
    def _select_tags_v2(self, sports_config: List[Dict], tag_counter: Counter) -> Dict[str, List[str]]:
        """
        选择 tag (重复次数 <= 2)
        
        Returns
        -------
        Dict[str, List[str]]
            {sport_key: [tag1, tag2, ...]}
        """
        result = {}
        
        for sport in sports_config:
            sport_key = sport.get('sport', '')
            tags_str = sport.get('tags', '')
            
            if not sport_key or not tags_str:
                continue
            
            tag_list = tags_str.split(',')
            
            # 选择重复次数 <= 2 的 tag
            selected_tags = [tag for tag in tag_list if tag_counter[tag] <= 2]
            
            if selected_tags:
                result[sport_key] = selected_tags
                self._log.debug(f'{sport_key}: {len(selected_tags)} 个 tag (重复 <= 2)')
        
        return result
    
    def _get_events(self, tag_id: str, active_only: bool) -> List[MarketEvent]:
        """获取事件"""
        
        try:
            params = {
                'tag_id': tag_id,
                'closed': 'false' if active_only else 'true',
                'limit': 10000
            }
            
            response = self.session.get(f'{self.gamma_api}/events', params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            if not isinstance(data, list):
                return []
            
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
        
        if not title or 'More Markets' in title:
            return None
        
        # 提取主客队
        home_team, away_team = self._extract_teams(title)
        
        if not home_team or not away_team:
            return None
        
        # 从 series 获取 API competition
        series = data.get('series', [])
        api_competition = ''
        
        if isinstance(series, list) and len(series) > 0:
            first_series = series[0]
            if isinstance(first_series, dict):
                api_competition = first_series.get('title', '')
        
        if not api_competition:
            return None
        
        # 匹配网页 competition
        web_competition = self._match_web_competition(api_competition)
        
        if not web_competition:
            return None
        
        # 获取 sport
        sport = self.web_competition_to_sport.get(web_competition, 'Unknown')
        
        # 使用网页的 competition (不是 API 的)
        competition = web_competition
        
        return MarketEvent(
            platform='Polymarket',
            event_id=str(data.get('id', '')),
            sport=sport,
            competition=competition,  # 使用网页 competition
            event=f'{home_team} vs {away_team}',
            home_team=home_team,
            away_team=away_team,
            metadata={
                'title': title,
                'start_date': data.get('startDate'),
                'api_competition': api_competition,  # 保存 API competition 用于调试
            }
        )
    
    def _extract_teams(self, title: str) -> tuple:
        """提取主客队"""
        
        # 优先 "vs."
        if ' vs. ' in title:
            parts = title.split(' vs. ', 1)
            if len(parts) == 2:
                home, away = parts[0].strip(), parts[1].strip()
                if ':' in home:
                    home = home.split(':')[-1].strip()
                return home, away
        
        # 其次 " vs "
        if ' vs ' in title:
            parts = title.split(' vs ', 1)
            if len(parts) == 2:
                home, away = parts[0].strip(), parts[1].strip()
                if ':' in home:
                    home = home.split(':')[-1].strip()
                return home, away
        
        # 正则匹配
        match = re.split(r'\s*vs\.?\s*', title, maxsplit=1, flags=re.IGNORECASE)
        if len(match) == 2:
            home, away = match[0].strip(), match[1].strip()
            if ':' in home:
                home = home.split(':')[-1].strip()
            return home, away
        
        return None, None
    
    def _match_web_competition(self, api_competition: str) -> Optional[str]:
        """匹配网页 competition"""
        
        if not api_competition:
            return None
        
        # 直接匹配
        for web_comp in self.web_competition_to_sport.keys():
            if api_competition.lower() == web_comp.lower():
                return web_comp
        
        # 相似度匹配
        best_match = None
        best_similarity = 0
        
        for web_comp in self.web_competition_to_sport.keys():
            similarity = self._get_similar(api_competition, web_comp)
            
            if similarity > best_similarity:
                best_similarity = similarity
                best_match = web_comp
        
        if best_similarity > 0:
            return best_match
        
        return None
    
    def _get_similar(self, str1: str, str2: str) -> int:
        """相似度"""
        elements1 = [e for e in re.split(r'[^a-zA-Z0-9]', str1) if len(e) > 1]
        elements2 = [e for e in re.split(r'[^a-zA-Z0-9]', str2) if len(e) > 1]
        
        if not elements1 or not elements2:
            return 0
        
        matches = 0
        
        for e1 in elements1:
            for e2 in elements2:
                if self._is_subsequence(e1.lower(), e2.lower()) or self._is_subsequence(e2.lower(), e1.lower()):
                    matches += 1
                    break
        
        return matches
    
    def _is_subsequence(self, s: str, t: str) -> bool:
        """子序列"""
        it = iter(t)
        return all(c in it for c in s)


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    print('Polymarket 市场发现 V2')
    print('=' * 70)
    
    client = PolymarketV2()
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
