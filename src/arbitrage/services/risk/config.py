"""
风控服务配置
"""

from dataclasses import dataclass, field


@dataclass
class RiskConfig:
    """
    风控配置

    Attributes:
        enabled: 是否启用风控
        match_sl: 单场比赛止损阈值（负数，如 -0.10 表示 -10%）
                  当某场比赛任一方向的持仓返水率低于此值时，停止该场比赛下注
        global_sl: 全局累计止损阈值（负数，如 -0.50 表示 -50%）
                   所有比赛的最低方向持仓返水率之和低于此值时，停止所有下注
        match_tp: 单场比赛止盈阈值（正数，如 0.10 表示 10%）
                  当某场比赛所有方向的持仓返水率都 >= 此值时，停止该场比赛下注
        match_overrides: 比赛级别的止损覆盖 {pair_id: sl_value}
    """
    enabled: bool = True
    execution_enabled: bool = True
    match_sl: float = -0.10  # -10%
    global_sl: float = -0.50  # -50%
    match_tp: float = 0.10   # 10%
    match_overrides: dict[str, float] = field(default_factory=dict)
    health_check_interval_sec: float = 30.0  # 健康检查间隔（秒）

    def get_match_sl(self, pair_id: str) -> float:
        """获取指定比赛的止损阈值"""
        return self.match_overrides.get(pair_id, self.match_sl)

    def update_from_dict(self, data: dict) -> None:
        """原地更新配置（保持引用不变）"""
        self.enabled = data.get("enabled", self.enabled)
        self.execution_enabled = data.get("execution_enabled", self.execution_enabled)
        self.match_sl = data.get("match_sl", self.match_sl)
        self.global_sl = data.get("global_sl", self.global_sl)
        self.match_tp = data.get("match_tp", self.match_tp)
        self.match_overrides = data.get("match_overrides", self.match_overrides)
        self.health_check_interval_sec = data.get("health_check_interval_sec", self.health_check_interval_sec)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "execution_enabled": self.execution_enabled,
            "match_sl": self.match_sl,
            "global_sl": self.global_sl,
            "match_tp": self.match_tp,
            "match_overrides": self.match_overrides,
            "health_check_interval_sec": self.health_check_interval_sec,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RiskConfig":
        return cls(
            enabled=data.get("enabled", True),
            execution_enabled=data.get("execution_enabled", True),
            match_sl=data.get("match_sl", -0.10),
            global_sl=data.get("global_sl", -0.50),
            match_tp=data.get("match_tp", 0.10),
            match_overrides=data.get("match_overrides", {}),
            health_check_interval_sec=data.get("health_check_interval_sec", 30.0),
        )
