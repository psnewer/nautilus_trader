"""
配置数据模型 - 在 Match 阶段进行预处理
"""

from dataclasses import dataclass, field, asdict
from typing import List, Dict
import yaml


@dataclass
class PolymarketCrawlerConfig:
    """Polymarket 爬虫配置"""
    enabled: bool = True
    sports: List[str] = field(default_factory=lambda: ['Soccer', 'Basketball', 'Tennis'])
    competitions: Dict[str, List[str]] = field(default_factory=dict)
    max_events_per_sport: int = 0
    max_events_per_competition: int = 100
    max_total_events: int = 1000
    rate_limit_seconds: float = 1.0


@dataclass
class OrbitExchCrawlerConfig:
    """OrbitExch 爬虫配置"""
    enabled: bool = True
    sports: List[str] = field(default_factory=lambda: ['American Football', 'Soccer'])
    competitions: Dict[str, List[str]] = field(default_factory=dict)
    max_competitions_per_sport: int = 5
    max_events_per_competition: int = 0
    headless: bool = False
    base_url: str = "https://orbitexch.com"


@dataclass
class MarketDiscoveryActorConfig:
    """市场发现 Actor 配置"""
    actor_id: str = "MarketDiscovery-001"
    polymarket: PolymarketCrawlerConfig = field(default_factory=PolymarketCrawlerConfig)
    orbitexch: OrbitExchCrawlerConfig = field(default_factory=OrbitExchCrawlerConfig)
    output_dir: str = "output"
    schedule_cron: str = "0 */6 * * *"


@dataclass
class PreprocessorConfig:
    """预处理器配置 - 在 Match 阶段使用"""
    enabled: bool = True
    normalize_sport: bool = True
    normalize_competition: bool = True
    normalize_team_names: bool = True
    strip_whitespace: bool = True
    
    # 映射表
    sport_mappings: Dict[str, str] = field(default_factory=lambda: {
        'Football': 'Soccer',
        'Association Football': 'Soccer',
        'Soccer': 'Soccer',
        'Basketball': 'Basketball',
        'Tennis': 'Tennis',
        'American Football': 'American Football',
    })
    
    competition_mappings: Dict[str, str] = field(default_factory=lambda: {
        'EPL': 'English Premier League',
        'Premier League': 'English Premier League',
        'English Premier League': 'English Premier League',
        'La Liga': 'Spanish La Liga',
        'Spanish La Liga': 'Spanish La Liga',
        'UCL': 'UEFA Champions League',
        'Champions League': 'UEFA Champions League',
        'UEFA Champions League': 'UEFA Champions League',
    })
    
    team_name_mappings: Dict[str, str] = field(default_factory=lambda: {
        'Man Utd': 'Manchester United',
        'Man United': 'Manchester United',
        'Manchester United': 'Manchester United',
        'Man City': 'Manchester City',
        'Manchester City': 'Manchester City',
    })


@dataclass
class MatcherConfig:
    """匹配器配置"""
    match_sport: bool = True
    match_competition: bool = True
    match_home_team: bool = True
    match_away_team: bool = True
    allow_team_swap: bool = True
    min_confidence: float = 0.6
    matcher_class: str = "DefaultMatcher"


@dataclass
class MarketMatchingActorConfig:
    """市场匹配 Actor 配置"""
    actor_id: str = "MarketMatching-001"
    verbose: bool = True
    preprocessor: PreprocessorConfig = field(default_factory=PreprocessorConfig)
    matcher: MatcherConfig = field(default_factory=MatcherConfig)


@dataclass
class WebPanelConfig:
    """Web 面板配置"""
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    auto_open_browser: bool = True


@dataclass
class SystemConfig:
    """系统配置"""
    instance_id: str = "nautilus-001"
    log_level: str = "INFO"
    
    market_discovery: MarketDiscoveryActorConfig = field(default_factory=MarketDiscoveryActorConfig)
    market_matching: MarketMatchingActorConfig = field(default_factory=MarketMatchingActorConfig)
    web_panel: WebPanelConfig = field(default_factory=WebPanelConfig)
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return asdict(self)
    
    def to_yaml(self) -> str:
        """导出为 YAML"""
        return yaml.dump(self.to_dict(), default_flow_style=False, allow_unicode=True)
    
    @classmethod
    def from_yaml_file(cls, filepath: str) -> 'SystemConfig':
        """从 YAML 文件加载"""
        with open(filepath, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        # 构建配置对象
        market_discovery = MarketDiscoveryActorConfig(
            actor_id=data.get('market_discovery', {}).get('actor_id', 'MarketDiscovery-001'),
            polymarket=PolymarketCrawlerConfig(
                **data.get('market_discovery', {}).get('polymarket', {})
            ),
            orbitexch=OrbitExchCrawlerConfig(
                **data.get('market_discovery', {}).get('orbitexch', {})
            ),
            output_dir=data.get('market_discovery', {}).get('output_dir', 'output'),
            schedule_cron=data.get('market_discovery', {}).get('schedule_cron', '0 */6 * * *')
        )
        
        market_matching = MarketMatchingActorConfig(
            actor_id=data.get('market_matching', {}).get('actor_id', 'MarketMatching-001'),
            verbose=data.get('market_matching', {}).get('verbose', True),
            preprocessor=PreprocessorConfig(
                **data.get('market_matching', {}).get('preprocessor', {})
            ),
            matcher=MatcherConfig(
                **data.get('market_matching', {}).get('matcher', {})
            )
        )
        
        web_panel = WebPanelConfig(
            **data.get('web_panel', {})
        )
        
        return cls(
            instance_id=data.get('instance_id', 'nautilus-001'),
            log_level=data.get('log_level', 'INFO'),
            market_discovery=market_discovery,
            market_matching=market_matching,
            web_panel=web_panel
        )
    
    def save_to_file(self, filepath: str):
        """保存到文件"""
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(self.to_yaml())


# 使用示例
if __name__ == "__main__":
    # 从 YAML 加载
    config = SystemConfig.from_yaml_file('config.yaml')
    
    # 访问配置
    print("Polymarket Sports:", config.market_discovery.polymarket.sports)
    print("OrbitExch Sports:", config.market_discovery.orbitexch.sports)
    
    # 访问预处理配置
    preprocessor = config.market_matching.preprocessor
    print("\nSport Mappings:")
    for original, standard in preprocessor.sport_mappings.items():
        print(f"  {original} → {standard}")
    
    print("\nCompetition Mappings:")
    for original, standard in preprocessor.competition_mappings.items():
        print(f"  {original} → {standard}")
