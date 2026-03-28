"""
应用状态管理

管理配置和运行时数据状态。
"""

import asyncio
import datetime
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 自动加载 .env 文件
try:
    from dotenv import load_dotenv
    # 从项目根目录加载 .env
    project_root = Path(__file__).parent.parent.parent.parent.parent
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        logging.getLogger(__name__).info(f"Loaded .env from {env_path}")
except ImportError:
    pass  # python-dotenv 未安装，跳过

from src.arbitrage.services.market_discovery.config import (
    MarketDiscoveryConfig,
    SportConfig,
)
from src.arbitrage.services.market_matching.config import MarketMatchingConfig
from src.arbitrage.services.odds_subscription.config import OddsSubscriptionConfig
from src.arbitrage.services.execution.config import ExecutionConfig
from src.arbitrage.services.strategy.config import StrategyServiceConfig
from src.arbitrage.services.risk.config import RiskConfig
from nautilus_trader.common.component import MessageBus, LiveClock
from nautilus_trader.model.identifiers import TraderId


@dataclass
class ArbitrageConfig:
    """
    套利配置

    Attributes:
        share: 持仓份额系数 (USDC)，用于计算订单大小
        fx: 英镑兑美元汇率 (GBP/USD)，用于 OrbitExch 订单计算
    """
    share: float = 30.0  # 持仓份额系数 (USDC)
    fx: float = 1.27  # 英镑兑美元汇率 (GBP/USD)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArbitrageConfig":
        """从字典创建配置实例"""
        return cls(
            share=data.get("share", 30.0),
            fx=data.get("fx", 1.27),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "share": self.share,
            "fx": self.fx,
        }

    def calculate_polymarket_size(self, probability: float) -> float:
        """
        计算 Polymarket 订单大小

        Polymarket 的 size 参数是 share 数量，直接使用配置的 share 值。

        Args:
            probability: 概率值 (0-100 scale，未使用，保留参数兼容性)

        Returns:
            订单大小 (share 数量)
        """
        return self.share

    def calculate_orbitexch_size(self, odds: float) -> float:
        """
        计算 OrbitExch 订单大小

        OrbitExch 的 size 参数是 stake（投入金额，英镑）。
        公式: stake = share / odds / fx
        - share: 30 (目标 share 数量，美元)
        - odds: 2.0 (十进制赔率)
        - fx: 1.27 (英镑兑美元汇率)
        - stake = 30 / 2.0 / 1.27 ≈ 11.81 (投入 11.81 GBP)

        Args:
            odds: 十进制赔率 (如 2.0)

        Returns:
            订单大小 (stake，投入金额，英镑)
        """
        if odds <= 0:
            return 0.0
        return self.share / odds / self.fx


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
        self._odds_config = OddsSubscriptionConfig()
        self._arbitrage_config = ArbitrageConfig()
        self._execution_config = ExecutionConfig()
        self._strategy_config = StrategyServiceConfig()
        self._risk_config = RiskConfig()

        # 运行时数据
        self._polymarket_events: list[DiscoveryResult] = []
        self._orbitexch_events: list[DiscoveryResult] = []
        self._matched_pairs: list[MatchedPairResult] = []
        self._matched_pairs_full: list = []  # 保存完整的 MatchedPair 对象（用于赔率订阅）

        # 赔率订阅服务
        self._odds_service = None  # 延迟初始化

        # 策略服务
        self._strategy_service = None  # 延迟初始化

        # 执行服务
        self._execution_service = None  # 延迟初始化

        # 风控服务
        self._risk_service = None  # 延迟初始化

        # 消息总线
        self._msgbus = None

        # 管道状态
        self._pipeline_active: bool = False
        self._pipeline_phase: str = "idle"  # idle | discovery | matching | subscribing | running
        self._pipeline_subscribed: bool = False

        # 定时调度器
        self._scheduler_task: asyncio.Task | None = None

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
                if "odds" in data:
                    self._odds_config = OddsSubscriptionConfig.from_dict(
                        data["odds"]
                    )
                if "arbitrage" in data:
                    self._arbitrage_config = ArbitrageConfig.from_dict(
                        data["arbitrage"]
                    )
                if "execution" in data:
                    self._execution_config = ExecutionConfig.from_dict(
                        data["execution"]
                    )
                if "strategy" in data:
                    self._strategy_config = StrategyServiceConfig.from_dict(
                        data["strategy"]
                    )
                if "risk" in data:
                    self._risk_config = RiskConfig.from_dict(data["risk"])

                self._log.info(f"Loaded config from {DEFAULT_CONFIG_PATH}")
            except Exception as e:
                self._log.warning(f"Failed to load saved config: {e}")

    def _save_config(self) -> None:
        """保存配置到文件"""
        try:
            data = {
                "discovery": self._discovery_config_to_dict(),
                "matching": self._matching_config_to_dict(),
                "odds": self._odds_config.to_dict(),
                "arbitrage": self._arbitrage_config.to_dict(),
                "execution": self._execution_config.to_dict(),
                "strategy": self._strategy_config.to_dict(),
                "risk": self._risk_config.to_dict(),
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
    # Odds 配置
    # =========================================================================

    def get_odds_config(self) -> dict:
        """获取赔率订阅配置"""
        return self._odds_config.to_dict()

    def update_odds_config(self, data: dict) -> dict:
        """更新赔率订阅配置"""
        self._odds_config = OddsSubscriptionConfig.from_dict(data)
        self._save_config()

        # 如果赔率服务已创建，更新其配置
        if self._odds_service is not None:
            self._odds_service.update_config(self._odds_config)

        return self.get_odds_config()

    @property
    def odds_config_obj(self) -> OddsSubscriptionConfig:
        """获取赔率订阅配置对象"""
        return self._odds_config

    # =========================================================================
    # Arbitrage 配置
    # =========================================================================

    def get_arbitrage_config(self) -> dict:
        """获取套利配置"""
        return self._arbitrage_config.to_dict()

    def update_arbitrage_config(self, data: dict) -> dict:
        """更新套利配置"""
        self._arbitrage_config = ArbitrageConfig.from_dict(data)
        self._save_config()

        # 同步更新策略服务的参数
        if self._strategy_service is not None:
            self._strategy_service.set_arbitrage_params(
                share=self._arbitrage_config.share,
            )

        # 同步 fx 到 RiskService
        if self._risk_service is not None:
            self._risk_service.set_fx(self._arbitrage_config.fx)

        return self.get_arbitrage_config()

    @property
    def arbitrage_config_obj(self) -> ArbitrageConfig:
        """获取套利配置对象"""
        return self._arbitrage_config

    # =========================================================================
    # Execution 配置
    # =========================================================================

    def get_execution_config(self) -> dict:
        """获取执行配置"""
        return self._execution_config.to_dict()

    def update_execution_config(self, data: dict) -> dict:
        """更新执行配置"""
        if "tracking_timeout_sec" in data:
            if data["tracking_timeout_sec"] is None:
                pass
            else:
                self._execution_config.tracking_timeout_sec = float(
                    data["tracking_timeout_sec"]
                )
        if "max_failure_retries" in data:
            if data["max_failure_retries"] is None:
                pass
            else:
                self._execution_config.max_failure_retries = int(
                    data["max_failure_retries"]
                )
        if "discount" in data and data["discount"] is not None:
            self._execution_config.discount = float(data["discount"])
        if "take_off" in data and data["take_off"] is not None:
            self._execution_config.take_off = float(data["take_off"])
        if "market_order_enabled" in data and data["market_order_enabled"] is not None:
            self._execution_config.market_order_enabled = bool(data["market_order_enabled"])

        self._save_config()

        if self._execution_service is not None:
            self._execution_service.update_config(self._build_execution_config())

        return self.get_execution_config()

    def _build_execution_config(self) -> ExecutionConfig:
        """构建执行配置（合并环境变量）"""
        base = self._execution_config
        return ExecutionConfig(
            enabled=base.enabled,
            polymarket_api_key=os.getenv("POLYMARKET_API_KEY", ""),
            polymarket_api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
            polymarket_passphrase=os.getenv("POLYMARKET_PASSPHRASE", ""),
            polymarket_clob_api_key=os.getenv("POLYMARKET_CLOB_API_KEY", ""),
            polymarket_clob_secret=os.getenv("POLYMARKET_CLOB_SECRET", ""),
            polymarket_clob_passphrase=os.getenv("POLYMARKET_CLOB_PASSPHRASE", ""),
            polymarket_private_key=os.getenv("POLYMARKET_PRIVATE_KEY", ""),
            polymarket_funder=os.getenv("POLYMARKET_FUNDER", ""),
            polymarket_clob_url=base.polymarket_clob_url,
            orbitexch_api_url=base.orbitexch_api_url,
            orbitexch_default_persistence=base.orbitexch_default_persistence,
            default_order_type=base.default_order_type,
            discount=base.discount,
            take_off=base.take_off,
            market_order_enabled=base.market_order_enabled,
            tracking_timeout_sec=base.tracking_timeout_sec,
            tracking_check_interval_sec=base.tracking_check_interval_sec,
            max_failure_retries=base.max_failure_retries,
        )

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

            # 使用存储的配置，并从环境变量补充凭据（如果配置中没有设置）
            config = self._odds_config
            config.orbitexch_username = os.getenv("ORBITEXCH_USERNAME", "")
            config.orbitexch_password = os.getenv("ORBITEXCH_PASSWORD", "")

            # Polymarket CLOB 凭据（用于 WS User Channel 和 REST 查询）
            config.polymarket_api_key = os.getenv("POLYMARKET_CLOB_API_KEY", "")
            config.polymarket_api_secret = os.getenv("POLYMARKET_CLOB_SECRET", "")
            config.polymarket_passphrase = os.getenv("POLYMARKET_CLOB_PASSPHRASE", "")
            config.polymarket_user_address = os.getenv(
                "POLYMARKET_USER_ADDRESS",
                os.getenv("POLYMARKET_ADDRESS", ""),
            )
            config.polymarket_funder = os.getenv("POLYMARKET_FUNDER", "")
            config.polymarket_eoa_address = os.getenv("POLYMARKET_EOA_ADDRESS", "")
            if config.polymarket_funder:
                config.polymarket_user_address = config.polymarket_funder

            self._odds_service = OddsSubscriptionService(
                config=config,
                logger=logging.getLogger("OddsSubscription"),
                msgbus=self.get_msgbus(),
            )

        return self._odds_service

    # =========================================================================
    # 策略服务
    # =========================================================================

    def get_strategy_config(self) -> dict:
        """获取策略配置"""
        return self._strategy_config.to_dict()

    def update_strategy_config(self, data: dict) -> dict:
        """更新策略配置"""
        self._strategy_config = StrategyServiceConfig.from_dict(data)
        self._save_config()

        # 如果策略服务已创建，更新其配置
        if self._strategy_service is not None:
            self._strategy_service.update_config(self._strategy_config)

        return self.get_strategy_config()

    @property
    def strategy_config_obj(self) -> StrategyServiceConfig:
        """获取策略配置对象"""
        return self._strategy_config

    def get_strategy_service(self):
        """
        获取策略服务实例

        延迟初始化，只在首次调用时创建。
        回调注册由 ensure_strategy_registered() 统一处理。
        """
        if self._strategy_service is None:
            from src.arbitrage.services.strategy.service import StrategyService

            self._strategy_service = StrategyService(
                config=self._strategy_config,
                logger=logging.getLogger("StrategyService"),
                odds_service=self._odds_service,
                share=self._arbitrage_config.share,
                msgbus=self.get_msgbus(),
            )

        return self._strategy_service

    def get_msgbus(self):
        """获取消息总线"""
        if self._msgbus is None:
            self._msgbus = MessageBus(
                trader_id=TraderId("ARBITRAGE-001"),
                clock=LiveClock(),
                name="ArbitrageMessageBus",
            )
        return self._msgbus

    def ensure_strategy_registered(self) -> None:
        """
        确保策略服务已注册到赔率服务

        在赔率订阅开始前调用，确保策略服务能接收赔率更新。
        使用标志位防止重复注册回调。
        """
        odds_service = self.get_odds_service()
        strategy_service = self.get_strategy_service()

        # 确保策略服务有赔率服务的引用
        strategy_service.set_odds_service(odds_service)

        # 同步套利参数
        strategy_service.set_arbitrage_params(
            share=self._arbitrage_config.share,
        )

        # StrategyService 已在初始化时订阅消息总线

        # 注意：way_rebate 数据由 RiskService 计算，在 _evaluate_match 时注入到 context
        # 不需要注册 position_callback

    def register_matches_to_strategy(self) -> None:
        """
        将匹配的比赛注册到策略服务

        在赔率订阅之后调用，将比赛信息和状态同步到策略服务。
        """
        strategy_service = self.get_strategy_service()
        odds_service = self.get_odds_service()

        # 获取所有比赛状态
        match_statuses = odds_service.get_all_match_statuses()

        # 为每个匹配的比赛注册信息
        for pair in self._matched_pairs:
            is_live = match_statuses.get(pair.pair_id, False)
            strategy_service.register_match(
                pair_id=pair.pair_id,
                competition=pair.competition,
                home_team=pair.polymarket_home,  # 使用 Polymarket 队名作为标准
                away_team=pair.polymarket_away,
                is_live=is_live,
            )
            self._log.debug(
                f"Registered match {pair.pair_id}: {pair.polymarket_home} vs {pair.polymarket_away}, "
                f"is_live={is_live}"
            )

        self._log.info(f"Registered {len(self._matched_pairs)} matches to strategy service")

    def update_match_statuses(self) -> dict[str, bool]:
        """
        从赔率服务获取比赛状态并更新策略服务

        Returns:
            {pair_id: is_live}
        """
        odds_service = self.get_odds_service()
        strategy_service = self.get_strategy_service()

        # 获取最新比赛状态
        match_statuses = odds_service.get_all_match_statuses()

        # 通过消息总线广播比赛状态
        for pair_id, is_live in match_statuses.items():
            odds_service._on_match_status_update(pair_id, is_live)

        return match_statuses

    # =========================================================================
    # 执行服务
    # =========================================================================

    def get_execution_service(self):
        """
        获取执行服务实例

        延迟初始化，只在首次调用时创建。
        """
        if self._execution_service is None:
            from src.arbitrage.services.execution.service import ExecutionService

            # 从环境变量读取配置
            config = self._build_execution_config()

            self._execution_service = ExecutionService(
                config=config,
                logger=logging.getLogger("ExecutionService"),
            )

        return self._execution_service

    async def ensure_execution_registered(self) -> bool:
        """
        确保执行服务已初始化并连接到消息总线

        在赔率订阅开始前调用，确保执行服务能接收套利机会。

        Returns:
            是否初始化成功
        """
        odds_service = self.get_odds_service()
        execution_service = self.get_execution_service()

        # 初始化执行服务
        success = await execution_service.initialize()
        if not success:
            self._log.warning("Execution service initialization failed (may be disabled)")

        # 设置执行服务的依赖
        execution_service.set_odds_service(odds_service)
        execution_service.set_arbitrage_config(self._arbitrage_config)
        execution_service.set_msgbus(self.get_msgbus())

        # 传递 OrbitExch 页面引用（用于下单）
        orbitexch_pages = odds_service.get_orbitexch_pages()
        for competition_id, page in orbitexch_pages.items():
            if competition_id != "main":  # 跳过主登录页
                execution_service.set_orbitexch_page(competition_id, page)
                self._log.info(f"OrbitExch page set for competition: {competition_id}")

        # 初始化 risk service 的 cleanup 组件
        risk_service = self.get_risk_service()
        await risk_service.initialize_cleanup(self._build_execution_config())

        # 接线健康检查：risk_service 访问 execution_service 的 PM client 和 OE pages
        risk_service.set_execution_service(execution_service)
        risk_service.start_health_check_loop(
            self._risk_config.health_check_interval_sec
        )

        self._log.info("Execution service registered to message bus")
        return success

    def update_execution_pages(self) -> None:
        """
        更新执行服务的 OrbitExch 页面引用

        在赔率订阅完成后调用，确保执行服务有最新的页面引用。
        """
        if not self._execution_service or not self._odds_service:
            self._log.warning(
                f"Cannot update execution pages: "
                f"execution_service={bool(self._execution_service)}, "
                f"odds_service={bool(self._odds_service)}"
            )
            return

        orbitexch_pages = self._odds_service.get_orbitexch_pages()
        self._log.info(f"Found {len(orbitexch_pages)} OrbitExch pages to update")

        page_count = 0
        for competition_id, page in orbitexch_pages.items():
            if competition_id != "main":
                self._execution_service.set_orbitexch_page(competition_id, page)
                page_count += 1

        self._log.info(f"Updated {page_count} OrbitExch pages for execution service")

    # =========================================================================
    # 风控服务
    # =========================================================================

    def get_risk_config(self) -> dict:
        """获取风控配置"""
        return self._risk_config.to_dict()

    def update_risk_config(self, data: dict) -> dict:
        """更新风控配置"""
        self._risk_config = RiskConfig.from_dict(data)
        self._save_config()

        # 如果风控服务已创建，更新其配置
        if self._risk_service is not None:
            self._risk_service.update_config(self._risk_config)

        return self.get_risk_config()

    @property
    def risk_config_obj(self) -> RiskConfig:
        """获取风控配置对象"""
        return self._risk_config

    def get_risk_service(self):
        """
        获取风控服务实例

        延迟初始化，只在首次调用时创建。
        """
        if self._risk_service is None:
            from src.arbitrage.services.risk.service import RiskService

            self._risk_service = RiskService(
                config=self._risk_config,
                logger=logging.getLogger("RiskService"),
                share=self._arbitrage_config.share,
            )

        return self._risk_service

    def ensure_risk_registered(self) -> None:
        """
        确保风控服务已集成到消息总线

        在赔率订阅开始前调用，确保风控服务能接收持仓更新。
        """
        risk_service = self.get_risk_service()
        strategy_service = self.get_strategy_service()
        odds_service = self.get_odds_service()

        # 同步 share 参数
        risk_service.set_share(self._arbitrage_config.share)
        risk_service.set_fx(self._arbitrage_config.fx)

        # 设置策略服务的风控服务引用（check_risk 同步门控保留 DI）
        strategy_service.set_risk_service(risk_service)

        # 连接消息总线
        risk_service.set_msgbus(self.get_msgbus())
        risk_service.set_odds_service(odds_service)

        # 设置 OddsService 的 RiskService 引用（用于余额回调）
        odds_service.set_risk_service(risk_service)

        self._log.info("Risk service integrated with message bus")

    # =========================================================================
    # 定时调度器
    # =========================================================================

    def start_scheduler(self) -> None:
        """启动定时发现调度器"""
        if self._scheduler_task and not self._scheduler_task.done():
            self._log.warning("Scheduler already running")
            return
        try:
            loop = asyncio.get_running_loop()
            self._scheduler_task = loop.create_task(self._scheduler_loop())
            self._log.info("Discovery scheduler started")
        except RuntimeError:
            self._log.error("No running event loop for scheduler")

    def stop_scheduler(self) -> None:
        """停止定时发现调度器"""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            self._scheduler_task = None
            self._log.info("Discovery scheduler stopped")

    async def _scheduler_loop(self) -> None:
        """定时执行 Discovery 的后台循环"""
        self._log.info("Scheduler loop started: aligned to top of the hour")

        while True:
            try:
                now = datetime.datetime.now()
                next_hour = (
                    now.replace(minute=0, second=0, microsecond=0)
                    + datetime.timedelta(hours=1)
                )
                sleep_seconds = max(0.0, (next_hour - now).total_seconds())
                await asyncio.sleep(sleep_seconds)

                if not self._pipeline_active:
                    self._log.info("Scheduler: pipeline inactive, skipping discovery")
                    continue

                # 跳过：如果 discovery 正在运行
                from .routes.discovery import _discovery_running
                if _discovery_running:
                    self._log.info("Scheduler: discovery already running, skipping")
                    continue

                self._log.info("Scheduler: triggering hourly discovery")
                from .routes.discovery import _run_discovery
                await _run_discovery()

            except asyncio.CancelledError:
                self._log.info("Scheduler loop cancelled")
                break
            except Exception as e:
                self._log.error(f"Scheduler loop error: {e}")

    # =========================================================================
    # 管道协调器
    # =========================================================================

    @property
    def pipeline_active(self) -> bool:
        """管道是否正在运行"""
        return self._pipeline_active

    @property
    def pipeline_phase(self) -> str:
        """当前管道阶段"""
        return self._pipeline_phase

    def setup_pipeline_subscriptions(self) -> None:
        """订阅管道消息（幂等）"""
        if self._pipeline_subscribed:
            return

        from .topics import DISCOVERY_COMPLETE_TOPIC, MATCHING_COMPLETE_TOPIC

        msgbus = self.get_msgbus()
        msgbus.subscribe(DISCOVERY_COMPLETE_TOPIC, self._on_discovery_complete)
        msgbus.subscribe(MATCHING_COMPLETE_TOPIC, self._on_matching_complete)
        self._pipeline_subscribed = True
        self._log.info("Pipeline subscriptions set up")

    def start_pipeline(self) -> None:
        """启动管道"""
        self._pipeline_active = True
        self._pipeline_phase = "discovery"
        self.setup_pipeline_subscriptions()
        self._log.info("Pipeline started")

    def stop_pipeline(self) -> None:
        """停止管道"""
        self._pipeline_active = False
        self._pipeline_phase = "idle"
        self._log.info("Pipeline stopped")

    def _on_discovery_complete(self, msg) -> None:
        """Discovery 完成回调 — 触发 Matching"""
        if not self._pipeline_active:
            return
        self._log.info("Pipeline: Discovery complete, triggering matching...")
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._run_matching_cascade())
        except RuntimeError:
            self._log.error("Pipeline: No running event loop, cannot trigger matching cascade")
            self.stop_pipeline()

    def _on_matching_complete(self, msg) -> None:
        """Matching 完成回调 — 触发 OddsSubscription"""
        if not self._pipeline_active:
            return
        self._log.info(f"Pipeline: Matching complete ({msg.pairs_count} pairs), triggering subscription...")
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._run_subscription_cascade())
        except RuntimeError:
            self._log.error("Pipeline: No running event loop, cannot trigger subscription cascade")
            self.stop_pipeline()

    async def _run_matching_cascade(self) -> None:
        """级联执行 Matching"""
        self._pipeline_phase = "matching"
        try:
            from .routes.matching import _run_matching
            await _run_matching()
            # 检查是否有匹配结果；若无则停止管道
            if not self._matched_pairs:
                self._log.warning("Pipeline: No matched pairs, stopping pipeline")
                self._pipeline_active = False
                self._pipeline_phase = "idle"
        except Exception as e:
            self._log.error(f"Pipeline matching cascade failed: {e}")
            self._pipeline_phase = "idle"
            self._pipeline_active = False

    async def _run_subscription_cascade(self) -> None:
        """级联执行 OddsSubscription"""
        self._pipeline_phase = "subscribing"
        try:
            from .routes.odds import _run_subscription
            await _run_subscription()
            self._pipeline_phase = "running"
            self._log.info("Pipeline: All stages complete, now running")
        except Exception as e:
            self._log.error(f"Pipeline subscription cascade failed: {e}")
            self._pipeline_phase = "idle"
            self._pipeline_active = False

    async def load_risk_historical_positions(self) -> dict[str, int]:
        """
        加载历史持仓到风控服务

        在赔率订阅完成后调用，从 API 获取历史持仓数据。

        Returns:
            {"polymarket": count, "orbitexch": count}
        """
        odds_service = self.get_odds_service()
        risk_service = self.get_risk_service()

        # 等待 Polymarket positions 到达
        if not await odds_service.wait_for_polymarket_positions(timeout=30.0):
            self._log.warning("Polymarket positions not received within timeout")

        # 等待 OrbitExch CURRENT_BETS 到达
        if not await odds_service.wait_for_orbitexch_current_bets(timeout=30.0):
            self._log.warning("OrbitExch CURRENT_BETS not received within timeout")

        # 重新 fetch positions（此时 tokens 已订阅，event_id/market_type 可正确填充）
        await odds_service.refresh_all_positions_and_orders()

        # 获取映射数据
        mappings = odds_service.get_position_mappings()

        # 获取持仓数据
        polymarket_positions = odds_service.get_polymarket_positions()
        orbitexch_bets = odds_service.get_orbitexch_bets()

        # 加载历史持仓
        result = risk_service.load_historical_positions(
            polymarket_positions=polymarket_positions,
            orbitexch_bets=orbitexch_bets,
            polymarket_pair_mapping=mappings["polymarket_pair_mapping"],
            orbitexch_pair_mapping=mappings["orbitexch_pair_mapping"],
            selection_mappings=mappings["selection_mappings"],
        )

        self._log.info(
            f"Loaded historical positions: Polymarket={result['polymarket']}, "
            f"OrbitExch={result['orbitexch']}"
        )

        return result


# 全局状态实例
app_state = AppState()


def get_risk_service():
    """全局获取风控服务（供路由使用）"""
    return app_state.get_risk_service()


def get_arbitrage_config():
    """全局获取套利配置（供路由使用）"""
    return app_state.arbitrage_config_obj
