"""
预处理器模块
提供事件数据的标准化和清洗功能
"""

from typing import List, Dict, Callable
from dataclasses import dataclass


@dataclass
class PreprocessConfig:
    """预处理配置"""
    normalize_sport: bool = True          # 标准化 sport 名称
    normalize_competition: bool = False   # 标准化 competition 名称
    normalize_team_names: bool = False    # 标准化队伍名称
    strip_whitespace: bool = True         # 去除多余空格
    verbose: bool = False                 # 显示详细信息


class EventPreprocessor:
    """事件预处理器"""
    
    # Sport 名称映射表
    SPORT_MAPPING = {
        'Football': 'Soccer',
        'Association Football': 'Soccer',
        'American Football': 'American Football',
        'Gridiron': 'American Football',
    }
    
    # Competition 名称映射表（可扩展）
    COMPETITION_MAPPING = {
        'EPL': 'English Premier League',
        'UCL': 'UEFA Champions League',
        'La Liga': 'LaLiga',
    }
    
    # 队名简写映射表（可扩展）
    TEAM_NAME_MAPPING = {
        'Man Utd': 'Manchester United',
        'Man City': 'Manchester City',
        'Tottenham': 'Tottenham Hotspur',
    }
    
    def __init__(self, config: PreprocessConfig = None):
        self.config = config or PreprocessConfig()
        self.stats = {
            'sport_normalized': 0,
            'competition_normalized': 0,
            'team_names_normalized': 0,
        }
    
    def preprocess_events(self, events: List, event_type: str = '') -> List:
        """
        预处理事件列表
        
        Args:
            events: MarketEvent 列表
            event_type: 事件类型描述（如 'Polymarket', 'OrbitExch'）
        
        Returns:
            预处理后的事件列表
        """
        if self.config.verbose:
            print(f"\n预处理 {event_type} 事件...")
        
        # 重置统计
        self.stats = {
            'sport_normalized': 0,
            'competition_normalized': 0,
            'team_names_normalized': 0,
        }
        
        for event in events:
            # 1. 去除空格
            if self.config.strip_whitespace:
                event.sport = event.sport.strip()
                event.competition = event.competition.strip()
                event.home_team = event.home_team.strip()
                event.away_team = event.away_team.strip()
            
            # 2. 标准化 sport
            if self.config.normalize_sport:
                original_sport = event.sport
                event.sport = self._normalize_sport(event.sport)
                if event.sport != original_sport:
                    self.stats['sport_normalized'] += 1
            
            # 3. 标准化 competition
            if self.config.normalize_competition:
                original_comp = event.competition
                event.competition = self._normalize_competition(event.competition)
                if event.competition != original_comp:
                    self.stats['competition_normalized'] += 1
            
            # 4. 标准化队名
            if self.config.normalize_team_names:
                original_home = event.home_team
                original_away = event.away_team
                event.home_team = self._normalize_team_name(event.home_team)
                event.away_team = self._normalize_team_name(event.away_team)
                if event.home_team != original_home or event.away_team != original_away:
                    self.stats['team_names_normalized'] += 1
        
        # 显示统计
        if self.config.verbose:
            self._print_stats(events, event_type)
        
        return events
    
    def _normalize_sport(self, sport: str) -> str:
        """标准化 sport 名称"""
        return self.SPORT_MAPPING.get(sport, sport)
    
    def _normalize_competition(self, competition: str) -> str:
        """标准化 competition 名称"""
        return self.COMPETITION_MAPPING.get(competition, competition)
    
    def _normalize_team_name(self, team_name: str) -> str:
        """标准化队伍名称"""
        return self.TEAM_NAME_MAPPING.get(team_name, team_name)
    
    def _print_stats(self, events: List, event_type: str):
        """打印统计信息"""
        print(f"  {event_type} 预处理统计:")
        print(f"    Sport 标准化: {self.stats['sport_normalized']} 个")
        print(f"    Competition 标准化: {self.stats['competition_normalized']} 个")
        print(f"    队名标准化: {self.stats['team_names_normalized']} 个")
        
        # 显示 sport 分布
        sport_dist = {}
        for e in events:
            sport_dist[e.sport] = sport_dist.get(e.sport, 0) + 1
        
        if sport_dist:
            print(f"\n  {event_type} Sport 分布:")
            for sport, count in sorted(sport_dist.items()):
                print(f"    {sport}: {count}")
    
    @classmethod
    def add_sport_mapping(cls, from_name: str, to_name: str):
        """添加自定义 sport 映射"""
        cls.SPORT_MAPPING[from_name] = to_name
    
    @classmethod
    def add_competition_mapping(cls, from_name: str, to_name: str):
        """添加自定义 competition 映射"""
        cls.COMPETITION_MAPPING[from_name] = to_name
    
    @classmethod
    def add_team_name_mapping(cls, from_name: str, to_name: str):
        """添加自定义队名映射"""
        cls.TEAM_NAME_MAPPING[from_name] = to_name
