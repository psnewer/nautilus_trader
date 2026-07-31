"""Venue registry / capability helpers.

第二阶段先做静态 registry:保留真实 venue identity,把 enablement、factory 和 odds/size
类别判断集中到这里。设计见 docs/arbitrage/architectures/_cross-cutting/venues.md。
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal
from importlib import import_module
from typing import Literal

from src.arbitrage.config.schema import ArbConfig


POLYMARKET = "POLYMARKET"
ORBITEXCH = "ORBITEXCH"
SHARPEXCH = "SHARPEXCH"
SPORTS_CLIENT = "PMSPORTS"

_LEG_KEY_ALIASES = {
    "pm": POLYMARKET,
    "oe": ORBITEXCH,
    "se": SHARPEXCH,
}

_DECIMAL_ODDS_TICKS_CENTS = (
    *(
        tick
        for start, stop, step in (
            (101, 200, 1),
            (200, 300, 2),
            (300, 400, 5),
            (400, 600, 10),
            (600, 1000, 20),
            (1000, 2000, 50),
            (2000, 3000, 100),
            (3000, 5000, 200),
            (5000, 10000, 500),
            (10000, 100000, 1000),
        )
        for tick in range(start, stop, step)
    ),
    100000,
)


@dataclass(frozen=True)
class VenueDescriptor:
    venue_id: str
    config_key: str
    instrument_model: Literal["binary_option", "betting"]
    odds_model: Literal["probability", "decimal"]
    amounts_normalized_to_usd: bool
    stake_currency: str
    data_config_builder: str
    exec_config_builder: str | None
    discovery_config_builder: str | None
    data_factory: str | None
    exec_factory: str | None
    settlement_kind: Literal["none", "polymarket_ctf"] = "none"


@dataclass(frozen=True)
class DataSourceDescriptor:
    source_id: str
    config_key: str
    client_id: str
    provider: str
    data_config_builder: str
    data_factory: str


@dataclass(frozen=True, slots=True)
class LegEconomics:
    share_if_wins: float
    profit_if_wins: float
    loss_if_loses: float


class PositionOutcomeInvariantError(ValueError):
    """持仓无法映射到声明 outcome 时抛出，调用方必须 fail-closed。"""


VENUE_REGISTRY: dict[str, VenueDescriptor] = {
    POLYMARKET: VenueDescriptor(
        venue_id=POLYMARKET,
        config_key="polymarket",
        instrument_model="binary_option",
        odds_model="probability",
        amounts_normalized_to_usd=True,
        stake_currency="USD",
        data_config_builder="to_polymarket_data_client_config",
        exec_config_builder="to_polymarket_exec_client_config",
        discovery_config_builder=None,
        data_factory="nautilus_trader.adapters.polymarket.arb_factories:ArbPolymarketLiveDataClientFactory",
        exec_factory="nautilus_trader.adapters.polymarket.arb_factories:ArbPolymarketLiveExecClientFactory",
        settlement_kind="polymarket_ctf",
    ),
    ORBITEXCH: VenueDescriptor(
        venue_id=ORBITEXCH,
        config_key="orbitexch",
        instrument_model="betting",
        odds_model="decimal",
        amounts_normalized_to_usd=True,
        stake_currency="USD",
        data_config_builder="to_orbitexch_data_client_config",
        exec_config_builder="to_orbitexch_exec_client_config",
        discovery_config_builder="to_oe_scraper_config",
        data_factory="nautilus_trader.adapters.orbitexch.factories:OrbitExchLiveDataClientFactory",
        exec_factory="nautilus_trader.adapters.orbitexch.factories:ArbOrbitExchLiveExecClientFactory",
    ),
    SHARPEXCH: VenueDescriptor(
        venue_id=SHARPEXCH,
        config_key="sharpexch",
        instrument_model="betting",
        odds_model="decimal",
        amounts_normalized_to_usd=True,
        stake_currency="USD",
        data_config_builder="to_sharpexch_data_client_config",
        exec_config_builder="to_sharpexch_exec_client_config",
        discovery_config_builder="to_se_discovery_config",
        data_factory="nautilus_trader.adapters.sharpexch.factories:SharpExchLiveDataClientFactory",
        exec_factory="nautilus_trader.adapters.sharpexch.factories:ArbSharpExchLiveExecClientFactory",
    ),
}


DATA_SOURCE_REGISTRY: dict[str, DataSourceDescriptor] = {
    "sports_status": DataSourceDescriptor(
        source_id="sports_status",
        config_key="sports_status",
        client_id=SPORTS_CLIENT,
        provider="polymarket_sports",
        data_config_builder="to_sports_data_client_config",
        data_factory="nautilus_trader.adapters.polymarket.arb_factories:PolymarketSportsLiveDataClientFactory",
    ),
}


def all_venues() -> tuple[VenueDescriptor, ...]:
    return tuple(VENUE_REGISTRY.values())


def all_data_sources() -> tuple[DataSourceDescriptor, ...]:
    return tuple(DATA_SOURCE_REGISTRY.values())


def resolve_factory(path: str):
    module_name, attr = path.split(":", 1)
    return getattr(import_module(module_name), attr)


def descriptor_for(venue: str) -> VenueDescriptor:
    return VENUE_REGISTRY[str(venue).upper()]


def is_known_venue(venue: str) -> bool:
    return str(venue).upper() in VENUE_REGISTRY


def venue_id_from_instrument_id(instrument_id) -> str:
    """从 NT `InstrumentId` 或兼容字符串取真实 venue id。"""
    venue = getattr(instrument_id, "venue", None)
    value = getattr(venue, "value", None)
    if value is not None:
        return str(value).upper()

    text = str(instrument_id)
    if "." not in text:
        return ""
    candidate = text.rsplit(".", 1)[-1].upper()
    return candidate if candidate in VENUE_REGISTRY else ""


def venue_id_from_leg_key(leg_key: str) -> str | None:
    """从 opportunity leg_key 前缀解析真实 venue id。"""
    prefix = str(leg_key).split(":", 1)[0].lower()
    if prefix in _LEG_KEY_ALIASES:
        return _LEG_KEY_ALIASES[prefix]
    for descriptor in VENUE_REGISTRY.values():
        if prefix in {descriptor.venue_id.lower(), descriptor.config_key.lower()}:
            return descriptor.venue_id
    return None


def _is_enabled(cfg: ArbConfig, descriptor: VenueDescriptor) -> bool:
    section = getattr(cfg.venues, descriptor.config_key)
    return bool(section.enabled)


def _is_data_source_enabled(cfg: ArbConfig, descriptor: DataSourceDescriptor) -> bool:
    section = getattr(cfg.data_sources, descriptor.config_key)
    return bool(section.enabled) and str(section.provider) == descriptor.provider


def is_venue_enabled(cfg: ArbConfig, venue: str) -> bool:
    return _is_enabled(cfg, descriptor_for(venue))


def enabled_venues(cfg: ArbConfig) -> tuple[VenueDescriptor, ...]:
    return tuple(desc for desc in all_venues() if _is_enabled(cfg, desc))


def enabled_venue_ids(cfg: ArbConfig) -> tuple[str, ...]:
    return tuple(desc.venue_id for desc in enabled_venues(cfg))


def enabled_data_sources(cfg: ArbConfig) -> tuple[DataSourceDescriptor, ...]:
    return tuple(desc for desc in all_data_sources() if _is_data_source_enabled(cfg, desc))


def enabled_data_source_client_ids(cfg: ArbConfig) -> tuple[str, ...]:
    return tuple(desc.client_id for desc in enabled_data_sources(cfg))


def enabled_tradable_venues(cfg: ArbConfig) -> tuple[VenueDescriptor, ...]:
    """当前 registry 中的 enabled venue 都是可交易 venue;PMSPORTS 属 sports client,不在此表。"""
    return enabled_venues(cfg)


def enabled_tradable_venue_ids(cfg: ArbConfig) -> tuple[str, ...]:
    return tuple(desc.venue_id for desc in enabled_tradable_venues(cfg))


def enabled_settlement_venues(cfg: ArbConfig, settlement_kind: str) -> tuple[VenueDescriptor, ...]:
    return tuple(
        desc
        for desc in enabled_venues(cfg)
        if desc.settlement_kind == settlement_kind
    )


def enabled_sports_client_ids(cfg: ArbConfig) -> tuple[str, ...]:
    return enabled_data_source_client_ids(cfg)


def is_decimal_odds_venue(venue: str) -> bool:
    return descriptor_for(venue).odds_model == "decimal"


def is_probability_odds_venue(venue: str) -> bool:
    return descriptor_for(venue).odds_model == "probability"


def venue_preference_rank(venue: str) -> tuple[int, int, str]:
    """稳定排序用:同价时 probability venue 先于 decimal venue,再按 registry 顺序。"""
    venue_id = str(venue).upper()
    try:
        descriptor = descriptor_for(venue_id)
    except KeyError:
        return (99, 99, venue_id)
    odds_rank = 0 if descriptor.odds_model == "probability" else 1
    registry_rank = tuple(VENUE_REGISTRY).index(descriptor.venue_id)
    return (odds_rank, registry_rank, descriptor.venue_id)


def probability_from_price(venue: str, price: float, claim: str = "yes") -> float:
    """价格 → 隐含概率(venues.md §4;#228 claim 感知)。

    decimal venue 的 claim="no" 腿 book 存的是 lay 列原值(cache 只存 venue 原始价),
    其隐含概率是补集 1 − 1/price;全系统 claim 概率换算唯一的家。
    """
    if descriptor_for(venue).odds_model == "decimal":
        if str(claim or "").strip().lower() == "no":
            return 1.0 - 1.0 / price
        return 1.0 / price
    return price


def price_from_probability(venue: str, probability: float, claim: str = "yes") -> float:
    """隐含概率 → 价格,`probability_from_price` 的严格代数反解(book 深度概率编码,见
    docs/arbitrage/architectures/data/architecture.md)。decimal venue 的 claim="no" 腿:
    对补集反解,1 / (1 − probability)。"""
    if descriptor_for(venue).odds_model == "decimal":
        if str(claim or "").strip().lower() == "no":
            return 1.0 / (1.0 - probability)
        return 1.0 / probability
    return probability


def order_exposure_probability(venue: str, price: float, side: str) -> float:
    """订单实际增加的 outcome 敞口概率。SELL 增加报价 outcome 的互补敞口。"""
    probability = probability_from_price(venue, price, "yes")
    side_name = str(getattr(side, "name", side) or "").rsplit(".", 1)[-1].upper()
    return 1.0 - probability if side_name == "SELL" else probability


def qty_from_share(venue: str, share: float, price: float) -> float:
    if descriptor_for(venue).odds_model == "decimal":
        return share / price
    return share


def outcome_for_position(
    venue: str,
    outcomes: Collection[str],
    *,
    selection_role: str | None,
    claim: str | None,
    position_side: str,
) -> str | None:
    """将 NT 净 Position 映射到 pair 内的经济 outcome。"""
    normalized_outcomes = tuple(str(value).strip().lower() for value in outcomes)
    base_outcome = str(claim or selection_role or "").strip().lower()
    side = str(getattr(position_side, "name", position_side) or "").rsplit(".", 1)[-1].upper()
    if not base_outcome or base_outcome not in normalized_outcomes:
        return None
    if side == "LONG":
        return base_outcome
    if side == "SHORT" and not is_decimal_odds_venue(venue):
        raise PositionOutcomeInvariantError(
            f"probability venue position cannot be SHORT: venue={venue}, outcome={base_outcome}",
        )
    if side != "SHORT":
        return None
    if len(normalized_outcomes) != 2:
        return None
    return next(outcome for outcome in normalized_outcomes if outcome != base_outcome)


def leg_economics(
    venue: str,
    price: float,
    size: float,
    *,
    is_lay: bool = False,
) -> LegEconomics:
    """计算已完成 outcome 归属的单腿 share、盈利和亏损。"""
    if is_decimal_odds_venue(venue):
        if is_lay:
            return LegEconomics(
                share_if_wins=size * price,
                profit_if_wins=size,
                loss_if_loses=size * (price - 1.0),
            )
        return LegEconomics(
            share_if_wins=size * price,
            profit_if_wins=size * (price - 1.0),
            loss_if_loses=size,
        )
    if is_lay:
        raise ValueError("probability venue does not support lay position economics")
    return LegEconomics(
        share_if_wins=size,
        profit_if_wins=size * (1.0 - price),
        loss_if_loses=size * price,
    )


def order_liability(
    venue: str,
    quantity: float,
    price: float,
    *,
    is_lay: bool = False,
) -> float:
    """订单可能占用的最大本金,与持仓敞口使用同一 venue 经济口径。"""
    return leg_economics(venue, price, quantity, is_lay=is_lay).loss_if_loses


def order_required_balance(venue: str, quantity: float, price: float, side: str) -> float:
    """订单需要的可用余额；统一处理 probability SELL 与 decimal LAY。"""
    descriptor = descriptor_for(venue)
    side_name = str(getattr(side, "name", side) or "").rsplit(".", 1)[-1].upper()
    if descriptor.odds_model == "probability" and side_name == "SELL":
        return 0.0
    return order_liability(
        venue,
        quantity,
        price,
        is_lay=descriptor.odds_model == "decimal" and side_name == "SELL",
    )


def normalize_order_price(venue: str, price: float, side: str) -> float:
    """按 venue 下单规则把价格保守量化到合法档位。

    Decimal venue 使用 OE/SE 共用的分段赔率梯度。BACK/BUY 向上取整，LAY/SELL
    向下取整，避免量化后接受比计划更差的成交价格。Probability venue 原值返回。
    """
    if not is_decimal_odds_venue(venue):
        return float(price)

    side_name = str(getattr(side, "name", side) or "").rsplit(".", 1)[-1].upper()
    if side_name in {"BUY", "BACK"}:
        round_up = True
    elif side_name in {"SELL", "LAY"}:
        round_up = False
    else:
        raise ValueError(f"Unsupported order side for decimal odds: {side!r}")

    cents = Decimal(str(price)) * 100
    index = bisect_left(_DECIMAL_ODDS_TICKS_CENTS, cents)
    if index >= len(_DECIMAL_ODDS_TICKS_CENTS):
        index = len(_DECIMAL_ODDS_TICKS_CENTS) - 1
    elif not round_up and Decimal(_DECIMAL_ODDS_TICKS_CENTS[index]) != cents:
        index = max(0, index - 1)
    return _DECIMAL_ODDS_TICKS_CENTS[index] / 100.0
