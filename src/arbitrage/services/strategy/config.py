"""
套利策略服务配置

支持插拔式的 Signal 和 Strategy 配置：
- Signal: 命名的信号定义，包含默认参数
- Strategy: 命名的策略定义，由多个 Signal 组合
- Competition Config: 按联赛配置策略
- Match Config: 按比赛配置策略（覆盖联赛配置）
"""

from dataclasses import dataclass, field
from typing import Any


# =============================================================================
# Signal 定义
# =============================================================================

@dataclass
class SignalDefinition:
    """
    信号定义

    Attributes:
        name: 信号名称（唯一标识）
        params: 默认参数
        description: 描述
    """
    name: str
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "SignalDefinition":
        """从字典创建"""
        if isinstance(data, dict):
            return cls(
                name=name,
                params=data.get("params", data),  # 兼容直接传 params
                description=data.get("description", ""),
            )
        return cls(name=name)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "params": self.params,
            "description": self.description,
        }


# =============================================================================
# Strategy 定义
# =============================================================================

@dataclass
class StrategyDefinition:
    """
    策略定义

    策略是多个 Signal 的组合，所有 Signal 都为 True 时策略触发。

    Attributes:
        name: 策略名称（唯一标识）
        signals: 组成该策略的信号名称列表
        description: 描述
    """
    name: str
    signals: list[str] = field(default_factory=list)
    description: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "StrategyDefinition":
        """从字典创建"""
        if isinstance(data, dict):
            return cls(
                name=name,
                signals=data.get("signals", []),
                description=data.get("description", ""),
            )
        elif isinstance(data, list):
            # 兼容直接传 signals 列表
            return cls(name=name, signals=data)
        return cls(name=name)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "signals": self.signals,
            "description": self.description,
        }


# =============================================================================
# Match Config（比赛级别配置）
# =============================================================================

@dataclass
class MatchConfig:
    """
    比赛级别配置

    用于覆盖联赛默认配置。

    Attributes:
        competition: 联赛名称
        home: 主队名称
        away: 客队名称
        strategies: 使用的策略列表（覆盖联赛配置）
        signal_overrides: 信号参数覆盖
    """
    competition: str
    home: str
    away: str
    strategies: list[str] = field(default_factory=list)
    signal_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def match_key(self) -> str:
        """生成唯一匹配键"""
        return f"{self.competition}|{self.home}|{self.away}"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MatchConfig":
        """从字典创建"""
        return cls(
            competition=data.get("competition", ""),
            home=data.get("home", ""),
            away=data.get("away", ""),
            strategies=data.get("strategies", data.get("strategy", [])),
            signal_overrides=data.get("signal_overrides", data.get("signal", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "competition": self.competition,
            "home": self.home,
            "away": self.away,
            "strategies": self.strategies,
            "signal_overrides": self.signal_overrides,
        }


# =============================================================================
# 默认配置
# =============================================================================

DEFAULT_SIGNALS: dict[str, dict] = {
    "rebate": {
        "params": {"rate": 0.03},
        "description": "套利返水率信号，当返水率 > rate 时为 True。",
    },
    "mean_rebate": {
        "params": {"rate": 0.03},
        "description": "平均返水信号，各方向取最优价格，返水平均分配到所有方向。",
    },
    "pre-match": {
        "params": {},
        "description": "赛前盘信号，比赛未开始时为 True",
    },
    "live": {
        "params": {},
        "description": "赛中盘信号，比赛进行中时为 True",
    },
    "multi-way": {
        "params": {},
        "description": "多方向过滤信号，移除持仓返水率为负的方向",
    },
}

DEFAULT_STRATEGIES: dict[str, dict] = {
    "strategy_1": {
        "signals": ["rebate", "pre-match"],
        "description": "赛前套利策略",
    },
    "strategy_2": {
        "signals": ["rebate", "live"],
        "description": "赛中套利策略",
    },
    "default": {
        "signals": ["rebate"],
        "description": "默认策略（仅检查返水率）",
    },
    "mean": {
        "signals": ["mean_rebate"],
        "description": "平均返水策略，返水平均分配到所有方向",
    },
}


# =============================================================================
# 服务配置
# =============================================================================

@dataclass
class StrategyServiceConfig:
    """
    策略服务配置

    配置示例:
    ```yaml
    services:
      strategy:
        enabled: true
        default_strategies: ["default"]

        signals:
          rebate:
            params: {rate: 0.03}
          pre-match:
            params: {}
          live:
            params: {}

        strategies:
          strategy_1:
            signals: [rebate, pre-match]
          strategy_2:
            signals: [rebate, live]

        competition_config:
          "English Premier League": ["strategy_1", "strategy_2"]
          "Italian Serie A": ["strategy_1"]

        match_config:
          - competition: "English Premier League"
            home: "Tottenham Hotspur FC"
            away: "Newcastle United FC"
            strategies: ["strategy_1"]
            signal_overrides:
              rebate: {rate: 0.1}
    ```
    """
    enabled: bool = True
    default_strategies: list[str] = field(default_factory=lambda: ["default"])

    # Signal 定义
    signals: dict[str, SignalDefinition] = field(default_factory=dict)

    # Strategy 定义
    strategies: dict[str, StrategyDefinition] = field(default_factory=dict)

    # Competition 级别配置：competition_name -> [strategy_names]
    competition_config: dict[str, list[str]] = field(default_factory=dict)

    # Match 级别配置
    match_config: list[MatchConfig] = field(default_factory=list)

    def __post_init__(self):
        """初始化默认 signals 和 strategies，并补齐新增的默认项"""
        if not self.signals:
            self.signals = {
                name: SignalDefinition.from_dict(name, data)
                for name, data in DEFAULT_SIGNALS.items()
            }
        else:
            # 补齐新增的默认 signal（已持久化的配置可能缺少后续新增的 signal）
            for name, data in DEFAULT_SIGNALS.items():
                if name not in self.signals:
                    self.signals[name] = SignalDefinition.from_dict(name, data)

        if not self.strategies:
            self.strategies = {
                name: StrategyDefinition.from_dict(name, data)
                for name, data in DEFAULT_STRATEGIES.items()
            }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StrategyServiceConfig":
        """从字典创建配置实例"""
        # 解析 signals
        signals_data = data.get("signals", {})
        signals = {
            name: SignalDefinition.from_dict(name, sig_data)
            for name, sig_data in signals_data.items()
        } if signals_data else {}

        # 解析 strategies
        strategies_data = data.get("strategies", {})
        strategies = {
            name: StrategyDefinition.from_dict(name, strat_data)
            for name, strat_data in strategies_data.items()
        } if strategies_data else {}

        # 解析 match_config
        match_config_data = data.get("match_config", [])
        match_config = [
            MatchConfig.from_dict(m) for m in match_config_data
        ]

        return cls(
            enabled=data.get("enabled", True),
            default_strategies=data.get("default_strategies", ["default"]),
            signals=signals,
            strategies=strategies,
            competition_config=data.get("competition_config", {}),
            match_config=match_config,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "enabled": self.enabled,
            "default_strategies": self.default_strategies,
            "signals": {
                name: sig.to_dict() for name, sig in self.signals.items()
            },
            "strategies": {
                name: strat.to_dict() for name, strat in self.strategies.items()
            },
            "competition_config": self.competition_config,
            "match_config": [m.to_dict() for m in self.match_config],
        }

    # =========================================================================
    # 配置查询方法
    # =========================================================================

    def get_signal(self, name: str) -> SignalDefinition | None:
        """获取 Signal 定义"""
        return self.signals.get(name)

    def get_strategy(self, name: str) -> StrategyDefinition | None:
        """获取 Strategy 定义"""
        return self.strategies.get(name)

    def get_strategies_for_competition(self, competition: str) -> list[str]:
        """
        获取联赛使用的策略列表

        Args:
            competition: 联赛名称

        Returns:
            策略名称列表，如果没有配置则返回默认策略
        """
        return self.competition_config.get(competition, self.default_strategies)

    def get_match_config(
        self,
        competition: str,
        home: str,
        away: str,
    ) -> MatchConfig | None:
        """
        获取比赛级别配置

        Args:
            competition: 联赛名称
            home: 主队名称
            away: 客队名称

        Returns:
            比赛配置，如果没有则返回 None
        """
        for match in self.match_config:
            if (match.competition == competition and
                match.home == home and
                match.away == away):
                return match
        return None

    def get_strategies_for_match(
        self,
        competition: str,
        home: str,
        away: str,
    ) -> list[str]:
        """
        获取比赛使用的策略列表

        优先级：比赛配置 > 联赛配置 > 默认配置

        Args:
            competition: 联赛名称
            home: 主队名称
            away: 客队名称

        Returns:
            策略名称列表
        """
        # 先查比赛配置
        match_cfg = self.get_match_config(competition, home, away)
        if match_cfg and match_cfg.strategies:
            return match_cfg.strategies

        # 再查联赛配置
        return self.get_strategies_for_competition(competition)

    def get_signal_params(
        self,
        signal_name: str,
        competition: str | None = None,
        home: str | None = None,
        away: str | None = None,
    ) -> dict[str, Any]:
        """
        获取信号参数（支持覆盖）

        优先级：比赛覆盖 > 信号默认参数

        Args:
            signal_name: 信号名称
            competition: 联赛名称
            home: 主队名称
            away: 客队名称

        Returns:
            信号参数
        """
        # 获取默认参数
        signal = self.get_signal(signal_name)
        params = dict(signal.params) if signal else {}

        # 检查比赛级别覆盖
        if competition and home and away:
            match_cfg = self.get_match_config(competition, home, away)
            if match_cfg and signal_name in match_cfg.signal_overrides:
                params.update(match_cfg.signal_overrides[signal_name])

        return params

    # =========================================================================
    # 配置修改方法
    # =========================================================================

    def add_signal(self, name: str, params: dict, description: str = "") -> None:
        """添加 Signal 定义"""
        self.signals[name] = SignalDefinition(
            name=name, params=params, description=description
        )

    def add_strategy(self, name: str, signals: list[str], description: str = "") -> None:
        """添加 Strategy 定义"""
        self.strategies[name] = StrategyDefinition(
            name=name, signals=signals, description=description
        )

    def set_competition_strategies(self, competition: str, strategies: list[str]) -> None:
        """设置联赛策略"""
        self.competition_config[competition] = strategies

    def add_match_config(self, config: MatchConfig) -> None:
        """添加比赛配置"""
        # 移除已存在的同一比赛配置
        self.match_config = [
            m for m in self.match_config
            if m.match_key != config.match_key
        ]
        self.match_config.append(config)

    def remove_match_config(self, competition: str, home: str, away: str) -> bool:
        """移除比赛配置"""
        key = f"{competition}|{home}|{away}"
        original_len = len(self.match_config)
        self.match_config = [m for m in self.match_config if m.match_key != key]
        return len(self.match_config) < original_len
