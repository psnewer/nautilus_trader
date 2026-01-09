# -*- coding: utf-8 -*-
"""
Polymarket 市场发现 - V3 (清理主客队名称)
"""

import logging
import json
import requests
from typing import List, Optional, Dict, Tuple
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


class PolymarketV3:
    """Polymarket V3"""
    
    def __init__(self):
        self._log = logging.getLogger('Polymarket')
        self.gamma_api = 'https://gamma-api.polymarket.com'
        self.session = requests.Session()
        self.web_competition_to_sport = {}
    
    def discover_markets(self, active_only = True):
        """发现市场"""
        
        self._log.info('开始 Polymarket 市场发现')
        
        self._log.info('步骤 1: 爬取网页')
        self._scrape_sports_page()
        self._log.info(f'获取 {len(self.web_competition_to_sport)} 个映射')
        
        self._log.info('步骤 2: 获取 API 配置')
        sports_config = self._get_sports_config()
        if not sports_config:
            return []
        
        tag_counter = self._count_tags(sports_config)
        sport_tags_list = self._select_tags_v2(sports_config, tag_counter)
        
        self._log.info('步骤 4: 获取事件')
        all_events = []
        
        for sport_key, tags in sport_tags_list.items():
            for tag_id in sorted(tags, key=int):
                events = self._get_events(tag_id, active_only)
                
                if events:
                    all_events.extend(events)
                    self._log.info(f'{sport_key} (tag={tag_id}): {len(events)} 个事件')
                    break
        
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
    
    def _get_sports_config(self):
        """获取 API 配置"""
        try:
            response = self.session.get(f'{self.gamma_api}/sports', timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self._log.error(f'获取失败: {e}')
            return []
    
    def _count_tags(self, sports_config):
        """统计 tag"""
        tag_counter = Counter()
        for sport in sports_config:
            tags_str = sport.get('tags', '')
            if tags_str:
                tag_counter.update(tags_str.split(','))
        return tag_counter
    
    def _select_tags_v2(self, sports_config, tag_counter):
        """选择 tag (重复次数 <= 2)"""
        result = {}
        
        for sport in sports_config:
            sport_key = sport.get('sport', '')
            tags_str = sport.get('tags', '')
            
            if not sport_key or not tags_str:
                continue
            
            tag_list = tags_str.split(',')
            selected_tags = [tag for tag in tag_list if tag_counter[tag] <= 2]
            
            if selected_tags:
                result[sport_key] = selected_tags
        
        return result
    
    def _get_events(self, tag_id, active_only):
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
    
    def _parse_event(self, data):
        """解析事件"""
        
        title = data.get('title', '')
        
        if not title or 'More Markets' in title:
            return None
        
        home_team, away_team = self._extract_teams(title)
        
        if not home_team or not away_team:
            return None
        
        # 清理主客队名称
        home_team = self._clean_team_name(home_team, is_home=True)
        away_team = self._clean_team_name(away_team, is_home=False)
        
        series = data.get('series', [])
        if not series or len(series) == 0:
            return None
        
        event_tags = data.get('tags', [])
        api_competition = self._find_competition_from_tags(event_tags)
        
        if not api_competition:
            first_series = series[0]
            if isinstance(first_series, dict):
                api_competition = first_series.get('title', '')
        
        if not api_competition:
            return None
        
        web_competition = self._match_web_competition(api_competition)
        
        if not web_competition:
            return None
        
        sport = self.web_competition_to_sport.get(web_competition, 'Unknown')
        competition = web_competition
        
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
                'api_competition': api_competition,
            }
        )
    
    def _clean_team_name(self, team_name, is_home):
        """
        清理队名
        
        规则:
        - 主队: 去掉第一个 : 或 - 之前的内容
        - 客队: 去掉第一个 : 或 - 之后的内容
        - 去掉左右空格
        """
        
        if is_home:
            # 主队：找到第一个 : 或 -，去掉之前的
            for sep in [':', '-']:
                if sep in team_name:
                    idx = team_name.index(sep)
                    team_name = team_name[idx + 1:]
                    break
        else:
            # 客队：找到第一个 : 或 -，去掉之后的
            for sep in [':', '-']:
                if sep in team_name:
                    idx = team_name.index(sep)
                    team_name = team_name[:idx]
                    break
        
        return team_name.strip()
    
    def _find_competition_from_tags(self, event_tags):
        """从 event.tags 找 competition"""
        
        if not event_tags:
            return None
        
        for tag in event_tags:
            label = tag.get('label', '')
            
            for web_comp in self.web_competition_to_sport.keys():
                if label.lower() == web_comp.lower():
                    return label
        
        return None
    
    def _extract_teams(self, title):
        """提取主客队"""
        
        if ' vs. ' in title:
            parts = title.split(' vs. ', 1)
            if len(parts) == 2:
                home, away = parts[0].strip(), parts[1].strip()
                # 去掉标题前缀 (例如 "Brisbane International:")
                if ':' in home:
                    home = home.split(':')[-1].strip()
                return home, away
        
        if ' vs ' in title:
            parts = title.split(' vs ', 1)
            if len(parts) == 2:
                home, away = parts[0].strip(), parts[1].strip()
                if ':' in home:
                    home = home.split(':')[-1].strip()
                return home, away
        
        match = re.split(r'\s*vs\.?\s*', title, maxsplit=1, flags=re.IGNORECASE)
        if len(match) == 2:
            home, away = match[0].strip(), match[1].strip()
            if ':' in home:
                home = home.split(':')[-1].strip()
            return home, away
        
        return None, None
    
    def _match_web_competition(self, api_competition):
        """匹配网页 competition"""
        
        if not api_competition:
            return None
        
        # 1. 先完全相同（不区分大小写）
        for web_comp in self.web_competition_to_sport.keys():
            if api_competition.lower() == web_comp.lower():
                return web_comp
        
        # 2. 相似度匹配
        best_match = None
        best_score = (0, 0)
        
        for web_comp in self.web_competition_to_sport.keys():
            match_count, char_count = self._get_similar(api_competition, web_comp)
            
            if (match_count, char_count) > best_score:
                best_score = (match_count, char_count)
                best_match = web_comp
        
        if best_score[0] > 0:
            return best_match
        
        return None
    
    def _split_elements(self, text):
        """拆分字符串为元素"""
        
        parts = re.split(r'[^a-zA-Z0-9]', text)
        elements = []
        
        for part in parts:
            if not part:
                continue
            
            upper_count = sum(1 for c in part if c.isupper())
            
            if upper_count >= 2:
                elements.extend(list(part))
            else:
                elements.append(part)
        
        return elements
    
    def _get_similar(self, str1, str2) -> Tuple[int, int]:
        """相似度计算 (返回匹配数和字符数)"""
        
        elements1 = self._split_elements(str1)
        elements2 = self._split_elements(str2)
        
        if not elements1 or not elements2:
            return (0, 0)
        
        upper_letters1 = [e for e in elements1 if len(e) == 1 and e.isupper()]
        upper_letters2 = [e for e in elements2 if len(e) == 1 and e.isupper()]
        
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
                    if self._is_subsequence(e1, e2):
                        matched = True
                        matched_chars = len(e1)
                    elif self._is_subsequence(e2, e1):
                        matched = True
                        matched_chars = len(e2)
                
                if matched:
                    matches += 1
                    char_count += matched_chars
                    used_indices2.add(idx2)
                    break
        
        return (matches, char_count)
    
    def _is_subsequence(self, s, t):
        """子序列"""
        it = iter(t)
        return all(c in it for c in s)


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    print('Polymarket 市场发现 V3')
    print('=' * 70)
    
    client = PolymarketV3()
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
        
        with open('polymarket_events.json', 'w', encoding='utf-8') as f:
            json.dump([e.to_dict() for e in events], f, indent=2, ensure_ascii=False)
        
        print('\n保存到 polymarket_events.json')


if __name__ == '__main__':
    main()
