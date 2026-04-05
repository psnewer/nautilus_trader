"""
市场匹配服务配置

包含：
- sport 名称别名映射
- competition 名称别名映射
- 匹配算法配置
"""

from dataclasses import dataclass, field
from typing import Any


# =============================================================================
# 默认别名映射
# =============================================================================

DEFAULT_SPORT_ALIASES: dict[str, str] = {
    # Football/Soccer 统一为 Soccer
    "football": "Soccer",
    "Football": "Soccer",
    "FOOTBALL": "Soccer",
    "soccer": "Soccer",
    "SOCCER": "Soccer",
    # Basketball
    "basketball": "Basketball",
    "BASKETBALL": "Basketball",
    # Tennis
    "tennis": "Tennis",
    "TENNIS": "Tennis",
    # 其他保持原样（首字母大写）
}

DEFAULT_COMPETITION_ALIASES: dict[str, str] = {
    # English Premier League
    "EPL": "English Premier League",
    "epl": "English Premier League",
    "Premier League": "English Premier League",
    "premier league": "English Premier League",
    "Barclays Premier League": "English Premier League",
    # Italian Serie A
    "sea": "Italian Serie A",
    "SEA": "Italian Serie A",
    "Serie A": "Italian Serie A",
    "serie a": "Italian Serie A",
    "Italian Serie A TIM": "Italian Serie A",
    # Spanish La Liga
    "La Liga": "Spanish La Liga",
    "la liga": "Spanish La Liga",
    "LaLiga": "Spanish La Liga",
    "LALIGA": "Spanish La Liga",
    "Primera Division": "Spanish La Liga",
    # German Bundesliga
    "Bundesliga": "German Bundesliga",
    "bundesliga": "German Bundesliga",
    # French Ligue 1
    "Ligue 1": "French Ligue 1",
    "ligue 1": "French Ligue 1",
    "Ligue1": "French Ligue 1",
    # UEFA Champions League
    "UCL": "UEFA Champions League",
    "Champions League": "UEFA Champions League",
    "champions league": "UEFA Champions League",
    # NBA
    "nba": "NBA",
    # NFL
    "nfl": "NFL",
    # MLB
    "mlb": "MLB",
    # NHL
    "nhl": "NHL",
}


# =============================================================================
# 配置数据类
# =============================================================================

@dataclass
class MarketMatchingConfig:
    """
    市场匹配服务配置

    配置示例:
    ```yaml
    services:
      market_matching:
        enabled: true
        min_similarity: 1
        sport_aliases:
          Football: Soccer
        competition_aliases:
          EPL: English Premier League
        competition_max_matches:
          English Premier League: 10
          Italian Serie A: 5
    ```
    """
    enabled: bool = True
    min_similarity: int = 1  # 最小相似度阈值
    sport_aliases: dict[str, str] = field(default_factory=lambda: DEFAULT_SPORT_ALIASES.copy())
    competition_aliases: dict[str, str] = field(default_factory=lambda: DEFAULT_COMPETITION_ALIASES.copy())
    competition_max_matches: dict[str, int] = field(default_factory=dict)  # 每个 competition 的最大匹配数量

    def update_from_dict(self, data: dict[str, Any]) -> None:
        """原地更新配置（保持引用不变）"""
        new = MarketMatchingConfig.from_dict(data)
        self.enabled = new.enabled
        self.min_similarity = new.min_similarity
        self.sport_aliases = new.sport_aliases
        self.competition_aliases = new.competition_aliases
        self.competition_max_matches = new.competition_max_matches

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MarketMatchingConfig":
        """从字典创建配置实例"""
        # 严格使用配置文件中的值，不与默认值合并
        # 只有配置文件中没有这些字段时才使用默认值
        sport_aliases = data.get("sport_aliases")
        if sport_aliases is None:
            sport_aliases = DEFAULT_SPORT_ALIASES.copy()

        competition_aliases = data.get("competition_aliases")
        if competition_aliases is None:
            competition_aliases = DEFAULT_COMPETITION_ALIASES.copy()

        competition_max_matches = data.get("competition_max_matches", {})

        return cls(
            enabled=data.get("enabled", True),
            min_similarity=data.get("min_similarity", 1),
            sport_aliases=sport_aliases,
            competition_aliases=competition_aliases,
            competition_max_matches=competition_max_matches,
        )

    def add_sport_alias(self, alias: str, standard: str) -> None:
        """添加 sport 别名"""
        self.sport_aliases[alias] = standard

    def add_competition_alias(self, alias: str, standard: str) -> None:
        """添加 competition 别名"""
        self.competition_aliases[alias] = standard
