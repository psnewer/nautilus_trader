# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------------------------
#  Polymarket 市场发现 - 正确逻辑
# -------------------------------------------------------------------------------------------------

"""
Polymarket 市场发现的正确实现

流程:
1. 访问 https://polymarket.com/sports 获取所有运动类型
2. 点开每个运动，获取所有 competition
3. 从 /sports API 找到对应的 sport，获取 tags
4. 选择最少重复的 tag
5. 使用 tag_id 从 /events 获取事件
"""

import logging
import json
import requests
from typing import List, Optional, Dict
from dataclasses import dataclass, asdict, field
from collections import Counter
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


class PolymarketCorrect:
    """Polymarket 正确的市场发现实现"""
    
    def __init__(self):
        self._log = logging.getLogger('PolymarketCorrect')
        self.gamma_api = 'https://gamma-api.polymarket.com'
        self.session = requests.Session()
    
    def discover_markets(self, active_only: bool = True) -> List[MarketEvent]:
        """
        发现市场
        
        Parameters
        ----------
        active_only : bool
            只返回活跃市场（closed=false）
        
        Returns
        -------
        List[MarketEvent]
            市场事件列表
        """
        self._log.info('开始发现 Polymarket 市场...')
        
        # 步骤 1: 获取所有 sports 配置
        sports_config = self._get_sports_config()
        
        if not sports_config:
            self._log.error('无法获取 sports 配置')
            return []
        
        self._log.info(f'获取到 {len(sports_config)} 个运动配置')
        
        # 步骤 2: 为每个 sport 找到最少重复的 tag
        sport_tags = self._select_unique_tags(sports_config)
        
        self._log.info(f'为 {len(sport_tags)} 个运动选择了唯一 tag')
        
        # 步骤 3: 使用 tag_id 获取事件
        all_events = []
        
        for sport_name, tag_id in sport_tags.items():
            self._log.info(f'获取 {sport_name} 的事件 (tag_id={tag_id})...')
            
            events = self._get_events_by_tag(
                tag_id=tag_id,
                sport=sport_name,
                closed=not active_only
            )
            
            all_events.extend(events)
            self._log.info(f'  {sport_name}: {len(events)} 个事件')
        
        self._log.info(f'✅ 总计发现 {len(all_events)} 个事件')
        return all_events
    
    def _get_sports_config(self) -> List[Dict]:
        """获取 sports 配置"""
        
        url = f'{self.gamma_api}/sports'
        
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            
            if isinstance(data, list):
                return data
            else:
                self._log.error(f'期望列表，得到: {type(data)}')
                return []
        
        except Exception as e:
            self._log.error(f'获取 sports 配置失败: {e}')
            return []
    
    def _select_unique_tags(self, sports_config: List[Dict]) -> Dict[str, str]:
        """
        为每个 sport 选择最少重复的 tag
        
        Parameters
        ----------
        sports_config : List[Dict]
            Sports 配置列表
        
        Returns
        -------
        Dict[str, str]
            {sport_name: tag_id}
        """
        
        # 统计所有 tag 的出现次数
        tag_counter = Counter()
        
        for sport in sports_config:
            tags = sport.get('tags', '')
            if tags:
                tag_list = tags.split(',')
                tag_counter.update(tag_list)
        
        self._log.debug(f'Tag 统计: {tag_counter}')
        
        # 为每个 sport 选择最少重复的 tag
        result = {}
        
        for sport in sports_config:
            sport_name = sport.get('sport', '')
            tags_str = sport.get('tags', '')
            
            if not sport_name or not tags_str:
                continue
            
            # 分割 tags
            tag_list = tags_str.split(',')
            
            # 选择出现次数最少的 tag
            min_tag = min(tag_list, key=lambda t: tag_counter[t])
            
            result[sport_name] = min_tag
            
            self._log.debug(
                f'{sport_name}: tags={tag_list}, '
                f'selected={min_tag} (count={tag_counter[min_tag]})'
            )
        
        return result
    
    def _get_events_by_tag(
        self,
        tag_id: str,
        sport: str,
        closed: bool = False
    ) -> List[MarketEvent]:
        """
        通过 tag_id 获取事件
        
        Parameters
        ----------
        tag_id : str
            Tag ID
        sport : str
            运动名称
        closed : bool
            是否包含已关闭的市场
        
        Returns
        -------
        List[MarketEvent]
            事件列表
        """
        
        url = f'{self.gamma_api}/events'
        params = {
            'tag_id': tag_id,
            'closed': str(closed).lower()
        }
        
        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            
            if not isinstance(data, list):
                self._log.warning(f'期望列表，得到: {type(data)}')
                return []
            
            # 转换为 MarketEvent
            events = []
            
            for item in data:
                event = self._parse_event(item, sport)
                if event:
                    events.append(event)
            
            return events
        
        except Exception as e:
            self._log.error(f'获取 tag {tag_id} 的事件失败: {e}')
            return []
    
    def _parse_event(self, data: Dict, sport: str) -> Optional[MarketEvent]:
        """
        解析事件数据
        
        Parameters
        ----------
        data : Dict
            API 返回的事件数据
        sport : str
            运动名称
        
        Returns
        -------
        Optional[MarketEvent]
            解析后的事件
        """
        
        # 提取关键字段
        home_team = data.get('homeTeamName', '')
        away_team = data.get('awayTeamName', '')
        
        if not home_team or not away_team:
            return None
        
        # 构建事件名称
        event_name = f'{home_team} vs {away_team}'
        
        # 检测赛事
        competition = self._detect_competition(sport, data)
        
        # 映射 sport 名称
        sport_name = self._map_sport_name(sport)
        
        return MarketEvent(
            platform='Polymarket',
            event_id=data.get('id', ''),
            sport=sport_name,
            competition=competition,
            event=event_name,
            home_team=home_team,
            away_team=away_team,
            metadata={
                'start_date': data.get('startDate'),
                'end_date': data.get('endDate'),
                'active': data.get('active', False),
                'closed': data.get('closed', False),
                'league': data.get('league', ''),
                'markets': data.get('markets', []),
            }
        )
    
    def _map_sport_name(self, sport_code: str) -> str:
        """映射 sport 代码到标准名称"""
        
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
            'tennis': 'Tennis',
            'golf': 'Golf',
            'mma': 'MMA',
            'boxing': 'Boxing',
        }
        
        return mapping.get(sport_code.lower(), sport_code.title())
    
    def _detect_competition(self, sport_code: str, data: Dict) -> str:
        """检测赛事名称"""
        
        # 优先使用 league 字段
        league = data.get('league', '')
        if league:
            return league
        
        # 根据 sport_code 映射
        mapping = {
            'nfl': 'NFL',
            'nba': 'NBA',
            'ncaab': 'NCAA Basketball',
            'ncaaf': 'NCAA Football',
            'mlb': 'MLB',
            'nhl': 'NHL',
            'epl': 'English Premier League',
            'ucl': 'UEFA Champions League',
            'laliga': 'La Liga',
            'bundesliga': 'Bundesliga',
            'seriea': 'Serie A',
        }
        
        return mapping.get(sport_code.lower(), '')


def main():
    """测试"""
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    print('=' * 70)
    print('Polymarket 市场发现 - 正确实现')
    print('=' * 70)
    print()
    
    client = PolymarketCorrect()
    
    # 获取活跃市场
    events = client.discover_markets(active_only=True)
    
    print(f'\n✅ 发现 {len(events)} 个活跃事件')
    
    if events:
        # 示例
        print('\n示例事件 (前 10 个):')
        for i, e in enumerate(events[:10], 1):
            print(f'\n{i}. {e.event}')
            print(f'   Sport: {e.sport}')
            print(f'   Competition: {e.competition}')
            print(f'   Teams: {e.home_team} vs {e.away_team}')
            print(f'   Start: {e.metadata.get("start_date", "N/A")[:10] if e.metadata.get("start_date") else "N/A"}')
        
        # 统计
        sports = {}
        for e in events:
            sports[e.sport] = sports.get(e.sport, 0) + 1
        
        print('\n运动类型分布:')
        for sport, count in sorted(sports.items(), key=lambda x: -x[1]):
            print(f'   {sport}: {count}')
        
        # 按赛事统计
        comps = {}
        for e in events:
            comp = e.competition or 'Unknown'
            comps[comp] = comps.get(comp, 0) + 1
        
        print('\n赛事分布:')
        for comp, count in sorted(comps.items(), key=lambda x: -x[1])[:10]:
            print(f'   {comp}: {count}')
        
        # 保存
        output = [e.to_dict() for e in events]
        with open('polymarket_events.json', 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        
        print(f'\n✅ 已保存到 polymarket_events.json')
    
    else:
        print('\n⚠️  没有找到活跃事件')


if __name__ == '__main__':
    main()
