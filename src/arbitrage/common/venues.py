"""Venue registry / capability helpers.

第二阶段先做静态 registry:保留真实 venue identity,把 enablement、factory 和 odds/size
类别判断集中到这里。设计见 docs/arbitrage/architectures/_cross-cutting/venues.md。
"""

from __future__ import annotations

from dataclasses import dataclass
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


def probability_from_price(venue: str, price: float) -> float:
    if descriptor_for(venue).odds_model == "decimal":
        return 1.0 / price
    return price


def qty_from_share(venue: str, share: float, price: float) -> float:
    if descriptor_for(venue).odds_model == "decimal":
        return share / price
    return share
