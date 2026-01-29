"""
应用状态管理

管理配置和运行时数据状态。
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.arbitrage.services.market_discovery.config import (
    MarketDiscoveryConfig,
    SportConfig,
)
from src.arbitrage.services.market_matching.config import MarketMatchingConfig


# 默认配置文件路径
DEFAULT_CONFIG_PATH = Path(__file__).parent / "default_config.json"


@dataclass
class DiscoveryResult:
    """市场发现结果"""
    venue: str
    sport: str
    competition: str
    home_team: str
    away_team: str
    event_id: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class MatchedPairResult:
    """匹配结果"""
    pair_id: str
    sport: str
    competition: str
    polymarket_home: str
    polymarket_away: str
    polymarket_event_id: str
    orbitexch_home: str
    orbitexch_away: str
    similarity: int
    confidence: float
    extra: dict = field(default_factory=dict)  # 用于存储额外信息 (sport_id, competition_id等)


class AppState:
    """
    应用状态管理器

    单例模式，管理所有配置和运行时数据。
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._log = logging.getLogger(self.__class__.__name__)

        # 配置
        self._discovery_config = MarketDiscoveryConfig()
        self._matching_config = MarketMatchingConfig()

        # 运行时数据
        self._polymarket_events: list[DiscoveryResult] = []
        self._orbitexch_events: list[DiscoveryResult] = []
        self._matched_pairs: list[MatchedPairResult] = []
        self._matched_pairs_full: list = []  # 保存完整的 MatchedPair 对象（用于赔率订阅）

        # 赔率订阅服务
        self._odds_service = None  # 延迟初始化

        # 加载保存的配置
        self._load_saved_config()

        self._initialized = True

    def _load_saved_config(self) -> None:
        """加载保存的配置"""
        if DEFAULT_CONFIG_PATH.exists():
            try:
                with open(DEFAULT_CONFIG_PATH) as f:
                    data = json.load(f)

                if "discovery" in data:
                    self._discovery_config = MarketDiscoveryConfig.from_dict(
                        data["discovery"]
                    )
                if "matching" in data:
                    self._matching_config = MarketMatchingConfig.from_dict(
                        data["matching"]
                    )

                self._log.info(f"Loaded config from {DEFAULT_CONFIG_PATH}")
            except Exception as e:
                self._log.warning(f"Failed to load saved config: {e}")

    def _save_config(self) -> None:
        """保存配置到文件"""
        try:
            data = {
                "discovery": self._discovery_config_to_dict(),
                "matching": self._matching_config_to_dict(),
            }
            with open(DEFAULT_CONFIG_PATH, "w") as f:
                json.dump(data, f, indent=2)
            self._log.info(f"Saved config to {DEFAULT_CONFIG_PATH}")
        except Exception as e:
            self._log.error(f"Failed to save config: {e}")

    def _discovery_config_to_dict(self) -> dict:
        """将 discovery 配置转为字典"""
        cfg = self._discovery_config
        return {
            "enabled": cfg.enabled,
            "poll_interval_sec": cfg.poll_interval_sec,
            "venues": {
                "polymarket": {
                    "enabled": cfg.venues.polymarket.enabled,
                    "sports": [
                        {"sport": s.sport, "competitions": s.competitions}
                        for s in cfg.venues.polymarket.sports
                    ],
                },
                "orbitexch": {
                    "enabled": cfg.venues.orbitexch.enabled,
                    "sports": [
                        {"sport": s.sport, "competitions": s.competitions}
                        for s in cfg.venues.orbitexch.sports
                    ],
                },
            },
        }

    def _matching_config_to_dict(self) -> dict:
        """将 matching 配置转为字典"""
        cfg = self._matching_config
        return {
            "enabled": cfg.enabled,
            "min_similarity": cfg.min_similarity,
            "sport_aliases": cfg.sport_aliases,
            "competition_aliases": cfg.competition_aliases,
            "competition_max_matches": cfg.competition_max_matches,
        }

    # =========================================================================
    # Discovery 配置
    # =========================================================================

    def get_discovery_config(self) -> dict:
        """获取市场发现配置"""
        return self._discovery_config_to_dict()

    def update_discovery_config(self, data: dict) -> dict:
        """更新市场发现配置"""
        self._discovery_config = MarketDiscoveryConfig.from_dict(data)
        self._save_config()
        return self.get_discovery_config()

    def update_venue_sports(
        self,
        venue: str,
        sports: list[dict],
    ) -> dict:
        """更新指定平台的 sports 配置"""
        sport_configs = [
            SportConfig(sport=s["sport"], competitions=s.get("competitions", []))
            for s in sports
        ]

        if venue == "polymarket":
            self._discovery_config.venues.polymarket.sports = sport_configs
        elif venue == "orbitexch":
            self._discovery_config.venues.orbitexch.sports = sport_configs
        else:
            raise ValueError(f"Unknown venue: {venue}")

        self._save_config()
        return self.get_discovery_config()

    # =========================================================================
    # Matching 配置
    # =========================================================================

    def get_matching_config(self) -> dict:
        """获取市场匹配配置"""
        return self._matching_config_to_dict()

    def update_matching_config(self, data: dict) -> dict:
        """更新市场匹配配置"""
        self._matching_config = MarketMatchingConfig.from_dict(data)
        self._save_config()
        return self.get_matching_config()

    def add_sport_alias(self, alias: str, standard: str) -> dict:
        """添加 sport 别名"""
        self._matching_config.sport_aliases[alias] = standard
        self._save_config()
        return self.get_matching_config()

    def add_competition_alias(self, alias: str, standard: str) -> dict:
        """添加 competition 别名"""
        self._matching_config.competition_aliases[alias] = standard
        self._save_config()
        return self.get_matching_config()

    # =========================================================================
    # Discovery 数据
    # =========================================================================

    def set_polymarket_events(self, events: list[DiscoveryResult]) -> None:
        """设置 Polymarket 发现的事件"""
        self._polymarket_events = events

    def set_orbitexch_events(self, events: list[DiscoveryResult]) -> None:
        """设置 OrbitExch 发现的事件"""
        self._orbitexch_events = events

    def get_discovery_results(self) -> dict:
        """获取市场发现结果"""
        return {
            "polymarket": [
                {
                    "venue": e.venue,
                    "sport": e.sport,
                    "competition": e.competition,
                    "home_team": e.home_team,
                    "away_team": e.away_team,
                    "event_id": e.event_id,
                }
                for e in self._polymarket_events
            ],
            "orbitexch": [
                {
                    "venue": e.venue,
                    "sport": e.sport,
                    "competition": e.competition,
                    "home_team": e.home_team,
                    "away_team": e.away_team,
                    "event_id": e.event_id,
                }
                for e in self._orbitexch_events
            ],
            "summary": {
                "polymarket_count": len(self._polymarket_events),
                "orbitexch_count": len(self._orbitexch_events),
            },
        }

    # =========================================================================
    # Matching 数据
    # =========================================================================

    def set_matched_pairs(self, pairs: list[MatchedPairResult]) -> None:
        """设置匹配结果"""
        self._matched_pairs = pairs

    def set_matched_pairs_full(self, pairs: list) -> None:
        """设置完整的 MatchedPair 对象（用于赔率订阅）"""
        self._matched_pairs_full = pairs

    def get_matching_results(self) -> dict:
        """获取市场匹配结果"""
        return {
            "pairs": [
                {
                    "pair_id": p.pair_id,
                    "sport": p.sport,
                    "competition": p.competition,
                    "polymarket": {
                        "home_team": p.polymarket_home,
                        "away_team": p.polymarket_away,
                        "event_id": p.polymarket_event_id,
                    },
                    "orbitexch": {
                        "home_team": p.orbitexch_home,
                        "away_team": p.orbitexch_away,
                        "sport_id": p.extra.get("sport_id", ""),
                        "competition_id": p.extra.get("competition_id", ""),
                    },
                    "similarity": p.similarity,
                    "confidence": p.confidence,
                }
                for p in self._matched_pairs
            ],
            "summary": {
                "total_pairs": len(self._matched_pairs),
            },
        }

    # =========================================================================
    # 获取内部对象（供服务调用）
    # =========================================================================

    @property
    def discovery_config_obj(self) -> MarketDiscoveryConfig:
        """获取 discovery 配置对象"""
        return self._discovery_config

    @property
    def matching_config_obj(self) -> MarketMatchingConfig:
        """获取 matching 配置对象"""
        return self._matching_config

    @property
    def polymarket_events(self) -> list[DiscoveryResult]:
        """获取 Polymarket 事件"""
        return self._polymarket_events

    @property
    def orbitexch_events(self) -> list[DiscoveryResult]:
        """获取 OrbitExch 事件"""
        return self._orbitexch_events

    @property
    def matched_pairs(self) -> list[MatchedPairResult]:
        """获取匹配的 pairs"""
        return self._matched_pairs

    @property
    def matched_pairs_full(self) -> list:
        """获取完整的 MatchedPair 对象"""
        return self._matched_pairs_full

    # =========================================================================
    # 赔率订阅服务
    # =========================================================================

    def get_odds_service(self):
        """
        获取赔率订阅服务实例

        延迟初始化，只在首次调用时创建
        """
        if self._odds_service is None:
            from src.arbitrage.services.odds_subscription.service import OddsSubscriptionService
            from src.arbitrage.services.odds_subscription.config import OddsSubscriptionConfig

            # 创建服务（使用默认配置）
            config = OddsSubscriptionConfig()
            self._odds_service = OddsSubscriptionService(
                config=config,
                logger=logging.getLogger("OddsSubscription"),
            )

        return self._odds_service


# 全局状态实例
app_state = AppState()
