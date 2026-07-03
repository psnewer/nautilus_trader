"""
ArbConfig loader —— JSON + env 凭证注入(Q23 C:env 优先覆盖 JSON 中的非空凭证值)。

设计见 `docs/arbitrage/architectures/_cross-cutting/configuration.md §5`。

顺序:
  1. 读 JSON → dict
  2. 检测 JSON 内是否含凭证字段(`venues.polymarket.{clob_api_*,...}` /
     `venues.{orbitexch,sharpexch}.{username,password}`)
     → 发 `ConfigWarning`(凭证应只走 env,§9 安全原则)
  3. 补缺省 venues 子 section,并从 env 覆盖凭证字段
  4. PM proxy 从 JSON 或 env 注入(不属于凭证)
  5. `msgspec.convert(dict, ArbConfig)` 校验 + 冻结
  6. 返回

错误路径:JSON 解析失败 / section 显式非 object / schema 不匹配 → `ConfigError`。
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

import msgspec

from src.arbitrage.config.schema import ArbConfig
from src.arbitrage.config.schema import ConfigError


class ConfigWarning(UserWarning):
    """JSON 中含本应只走 env 的凭证字段时发出。"""


# (env_var, target_path)
_OE_CRED_ENV: list[tuple[str, str]] = [
    ("ORBITEXCH_USERNAME", "username"),
    ("ORBITEXCH_PASSWORD", "password"),
]

_SE_CRED_ENV: list[tuple[str, str]] = [
    ("SHARPEXCH_USERNAME", "username"),
    ("SHARPEXCH_PASSWORD", "password"),
]

_PM_CRED_ENV: list[tuple[str, str]] = [
    ("POLYMARKET_CLOB_API_KEY", "clob_api_key"),
    ("POLYMARKET_CLOB_SECRET", "clob_api_secret"),         # 注意旧码用 _SECRET 非 _API_SECRET
    ("POLYMARKET_CLOB_PASSPHRASE", "clob_passphrase"),
    ("POLYMARKET_SIGNATURE_TYPE", "signature_type"),
    ("POLYMARKET_PRIVATE_KEY", "private_key"),
    ("POLYMARKET_FUNDER", "funder"),
    ("POLYMARKET_API_KEY", "builder_api_key"),              # builder relayer
    ("POLYMARKET_API_SECRET", "builder_api_secret"),
    ("POLYMARKET_PASSPHRASE", "builder_passphrase"),
]

_CREDENTIAL_FIELDS_PM = {p[1] for p in _PM_CRED_ENV if p[1] != "signature_type"}
_CREDENTIAL_FIELDS_OE = {p[1] for p in _OE_CRED_ENV}
_CREDENTIAL_FIELDS_SE = {p[1] for p in _SE_CRED_ENV}


def load_arb_config(path: str | Path) -> ArbConfig:
    """入口。详见模块 docstring。"""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid JSON in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be JSON object, got {type(raw).__name__}")

    _warn_credentials_in_json(raw)
    _inject_env_credentials(raw)
    _inject_env_proxy(raw)

    try:
        return msgspec.convert(raw, type=ArbConfig)
    except msgspec.ValidationError as e:
        raise ConfigError(f"schema mismatch in {path}: {e}") from e


def _warn_credentials_in_json(raw: dict) -> None:
    """如果 `venues.{polymarket,orbitexch,sharpexch}` 里有凭证字段非空 → 发 ConfigWarning。"""
    venues = _mapping_or_empty(raw.get("venues"))
    pm = _mapping_or_empty(venues.get("polymarket"))
    oe = _mapping_or_empty(venues.get("orbitexch"))
    se = _mapping_or_empty(venues.get("sharpexch"))
    leaked = []
    for k in _CREDENTIAL_FIELDS_PM:
        if pm.get(k):
            leaked.append(f"venues.polymarket.{k}")
    for k in _CREDENTIAL_FIELDS_OE:
        if oe.get(k):
            leaked.append(f"venues.orbitexch.{k}")
    for k in _CREDENTIAL_FIELDS_SE:
        if se.get(k):
            leaked.append(f"venues.sharpexch.{k}")
    if leaked:
        warnings.warn(
            f"credentials found in config JSON (should be env-only): {', '.join(leaked)}; "
            "rotate exposed credentials and migrate to env vars (see configuration.md §9)",
            ConfigWarning,
            stacklevel=3,
        )


def _inject_env_credentials(raw: dict) -> None:
    """env 凭证覆盖 raw['venues'].{polymarket,orbitexch,sharpexch} 字段(就地)。

    env 缺失 → 不覆盖,保留 JSON 值(或 None);**不验证存在**(下游 client 构造时 raise)。
    """
    venues = _ensure_mapping(raw, "venues")
    _ensure_mapping(venues, "polymarket", path="venues.polymarket")
    _ensure_mapping(venues, "orbitexch", path="venues.orbitexch")
    _ensure_mapping(venues, "sharpexch", path="venues.sharpexch")

    pm = venues["polymarket"]
    for env_name, field in _PM_CRED_ENV:
        val = os.environ.get(env_name)
        if val is not None:
            if field == "signature_type":
                val = int(val)
            pm[field] = val

    oe = venues["orbitexch"]
    for env_name, field in _OE_CRED_ENV:
        val = os.environ.get(env_name)
        if val is not None:
            oe[field] = val

    se = venues["sharpexch"]
    for env_name, field in _SE_CRED_ENV:
        val = os.environ.get(env_name)
        if val is not None:
            se[field] = val


def _inject_env_proxy(raw: dict) -> None:
    """PM CLOB WS 使用 NT pyo3 client,需显式 proxy_url;不读系统代理会导致直连超时。"""
    venues = _ensure_mapping(raw, "venues")
    _ensure_mapping(venues, "polymarket", path="venues.polymarket")

    pm = venues["polymarket"]
    if pm.get("proxy_url"):
        return

    for env_name in ("POLYMARKET_PROXY_URL", "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        val = os.environ.get(env_name)
        if val:
            pm["proxy_url"] = val
            return


def _ensure_mapping(parent: dict, key: str, *, path: str | None = None) -> dict:
    """确保配置 section 缺省时补空 object,但显式非 object 时 fail-fast。"""
    section_path = path or key
    if key not in parent:
        parent[key] = {}
    value = parent[key]
    if not isinstance(value, dict):
        raise ConfigError(
            f"config section {section_path} must be JSON object, got {type(value).__name__}",
        )
    return value


def _mapping_or_empty(value) -> dict:
    """warning 检查只读凭证字段;显式类型错误留给 `_ensure_mapping` 报 ConfigError。"""
    return value if isinstance(value, dict) else {}
