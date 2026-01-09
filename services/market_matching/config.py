"""
Market Matching Configuration
匹配配置文件
"""

from dataclasses import dataclass


@dataclass
class PreprocessConfig:
    """预处理配置"""
    normalize_sport: bool = True          # 标准化 sport 名称
    normalize_competition: bool = False   # 标准化 competition 名称
    normalize_team_names: bool = False    # 标准化队伍名称
    strip_whitespace: bool = True         # 去除多余空格
    verbose: bool = False                 # 显示详细信息


@dataclass
class MatchConfig:
    """匹配配置"""
    # 匹配字段
    match_sport: bool = True
    match_competition: bool = True
    match_home_team: bool = True
    match_away_team: bool = True
    
    # 匹配选项
    allow_team_swap: bool = True          # 允许主客队交换
    min_confidence: float = 0.6           # 最低置信度阈值
    
    # 输出选项
    verbose: bool = False                 # 详细输出
    
    # 匹配器选择
    matcher_class: str = "DefaultMatcher" # 使用的匹配器
    
    # 预处理配置
    enable_preprocess: bool = True        # 启用预处理
    preprocess_config: PreprocessConfig = None  # 预处理配置
    
    def __post_init__(self):
        """初始化后处理"""
        if self.preprocess_config is None:
            self.preprocess_config = PreprocessConfig(
                normalize_sport=True,
                normalize_competition=False,
                normalize_team_names=False,
                strip_whitespace=True,
                verbose=self.verbose  # 继承主配置的 verbose
            )


# 预定义配置
DEFAULT_CONFIG = MatchConfig()

STRICT_CONFIG = MatchConfig(
    allow_team_swap=False,
    min_confidence=0.8,
    verbose=True,
    enable_preprocess=True,
    preprocess_config=PreprocessConfig(
        normalize_sport=True,
        normalize_competition=True,  # 严格模式下也标准化 competition
        normalize_team_names=False,
        verbose=True
    )
)

LOOSE_CONFIG = MatchConfig(
    match_sport=False,
    match_competition=False,
    min_confidence=0.4,
    verbose=True,
    enable_preprocess=False  # 宽松模式下关闭预处理
)

# 无预处理配置（用于测试）
NO_PREPROCESS_CONFIG = MatchConfig(
    enable_preprocess=False,
    verbose=True
)
