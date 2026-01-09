"""
预处理器 - 在 Match 阶段根据配置文件进行名称标准化
"""

from typing import List, Dict
import logging


class EventPreprocessor:
    """事件预处理器 - 根据配置进行名称标准化"""
    
    def __init__(self, config):
        """
        Args:
            config: PreprocessorConfig 实例
        """
        self.config = config
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # 统计信息
        self.stats = {
            'sport_normalized': 0,
            'competition_normalized': 0,
            'team_names_normalized': 0,
        }
    
    def preprocess_events(self, events: List, platform_name: str = '') -> List:
        """
        预处理事件列表
        
        Args:
            events: MarketEvent 列表
            platform_name: 平台名称（用于日志）
        
        Returns:
            预处理后的事件列表
        """
        if not self.config.enabled:
            self.logger.info("预处理器已禁用，跳过预处理")
            return events
        
        if self.config.verbose:
            self.logger.info(f"开始预处理 {platform_name} 的 {len(events)} 个事件...")
        
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
                    self.logger.debug(f"Sport: '{original_sport}' → '{event.sport}'")
            
            # 3. 标准化 competition
            if self.config.normalize_competition:
                original_comp = event.competition
                event.competition = self._normalize_competition(event.competition)
                if event.competition != original_comp:
                    self.stats['competition_normalized'] += 1
                    self.logger.debug(f"Competition: '{original_comp}' → '{event.competition}'")
            
            # 4. 标准化队名
            if self.config.normalize_team_names:
                original_home = event.home_team
                original_away = event.away_team
                event.home_team = self._normalize_team_name(event.home_team)
                event.away_team = self._normalize_team_name(event.away_team)
                if event.home_team != original_home or event.away_team != original_away:
                    self.stats['team_names_normalized'] += 1
                    if event.home_team != original_home:
                        self.logger.debug(f"Team: '{original_home}' → '{event.home_team}'")
                    if event.away_team != original_away:
                        self.logger.debug(f"Team: '{original_away}' → '{event.away_team}'")
        
        # 显示统计
        if self.config.verbose:
            self._print_stats(platform_name)
        
        return events
    
    def _normalize_sport(self, sport: str) -> str:
        """
        标准化 sport 名称
        
        Args:
            sport: 原始 sport 名称
        
        Returns:
            标准化后的 sport 名称
        """
        # 查找映射表
        normalized = self.config.sport_mappings.get(sport)
        
        if normalized is None:
            # 如果映射表中没有，返回原始值并记录警告
            self.logger.warning(f"未找到 sport 映射: '{sport}'，保持原值")
            return sport
        
        return normalized
    
    def _normalize_competition(self, competition: str) -> str:
        """
        标准化 competition 名称
        
        Args:
            competition: 原始 competition 名称
        
        Returns:
            标准化后的 competition 名称
        """
        # 查找映射表
        normalized = self.config.competition_mappings.get(competition)
        
        if normalized is None:
            # 如果映射表中没有，返回原始值并记录警告
            self.logger.warning(f"未找到 competition 映射: '{competition}'，保持原值")
            return competition
        
        return normalized
    
    def _normalize_team_name(self, team_name: str) -> str:
        """
        标准化队名
        
        Args:
            team_name: 原始队名
        
        Returns:
            标准化后的队名
        """
        # 查找映射表
        normalized = self.config.team_name_mappings.get(team_name)
        
        if normalized is None:
            # 如果映射表中没有，返回原始值
            # 队名很多，不记录警告
            return team_name
        
        return normalized
    
    def _print_stats(self, platform_name: str):
        """打印统计信息"""
        print(f"\n预处理统计 ({platform_name}):")
        print(f"  Sport 标准化: {self.stats['sport_normalized']} 个")
        print(f"  Competition 标准化: {self.stats['competition_normalized']} 个")
        print(f"  队名标准化: {self.stats['team_names_normalized']} 个")


# 使用示例
if __name__ == "__main__":
    from config_schemas import SystemConfig, PreprocessorConfig
    from dataclasses import dataclass
    
    @dataclass
    class MarketEvent:
        platform: str
        event_id: str
        sport: str
        competition: str
        event: str
        home_team: str
        away_team: str
        metadata: dict
    
    # 加载配置
    config = SystemConfig.from_yaml_file('config.yaml')
    preprocessor_config = config.market_matching.preprocessor
    
    # 创建预处理器
    preprocessor = EventPreprocessor(preprocessor_config)
    
    # 测试数据
    events = [
        MarketEvent(
            platform="Polymarket",
            event_id="1",
            sport="Football",  # 原始名称
            competition="EPL",  # 原始名称
            event="Man Utd vs Man City",
            home_team="Man Utd",  # 原始名称
            away_team="Man City",  # 原始名称
            metadata={}
        )
    ]
    
    print("预处理前:")
    print(f"  Sport: {events[0].sport}")
    print(f"  Competition: {events[0].competition}")
    print(f"  Home Team: {events[0].home_team}")
    print(f"  Away Team: {events[0].away_team}")
    
    # 预处理
    preprocessor.preprocess_events(events, "Polymarket")
    
    print("\n预处理后:")
    print(f"  Sport: {events[0].sport}")  # Soccer
    print(f"  Competition: {events[0].competition}")  # English Premier League
    print(f"  Home Team: {events[0].home_team}")  # Manchester United
    print(f"  Away Team: {events[0].away_team}")  # Manchester City
