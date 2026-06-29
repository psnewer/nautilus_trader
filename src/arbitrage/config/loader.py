"""
ArbConfig loader —— JSON + env 凭证注入(Q23 C:env 优先,JSON fallback)。

设计见 `docs/arbitrage/architectures/_cross-cutting/configuration.md §5`。

顺序:
  1. 读 JSON → dict
  2. 检测 JSON 内是否含凭证字段(`venues.polymarket.{clob_api_*,...}` / `venues.orbitexch.{username,password}`)
     → 发 `ConfigWarning`(凭证应只走 env,§9 安全原则)
  3. 凭证字段从 env 覆盖(沿用旧 `state.py` 变量名,用户 `.env` 不用改)
  4. PM proxy 从 JSON 或 env 注入(不属于凭证)
  5. `msgspec.convert(dict, ArbConfig)` 校验 + 冻结
  6. 返回

错误路径:JSON 解析失败 / schema 不匹配 → `ConfigError`(原异常 chained)。
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


# (env_var, fallback_env_var | None, target_path)
# fallback_env_var:旧码 `POLYMARKET_USER_ADDRESS` 同时认 `POLYMARKET_ADDRESS` 别名
_OE_CRED_ENV: list[tuple[str, str]] = [
    ("ORBITEXCH_USERNAME", "username"),
    ("ORBITEXCH_PASSWORD", "password"),
]

_PM_CRED_ENV: list[tuple[str, str | None, str]] = [
    ("POLYMARKET_CLOB_API_KEY", None, "clob_api_key"),
    ("POLYMARKET_CLOB_SECRET", None, "clob_api_secret"),         # 注意旧码用 _SECRET 非 _API_SECRET
    ("POLYMARKET_CLOB_PASSPHRASE", None, "clob_passphrase"),
    ("POLYMARKET_SIGNATURE_TYPE", None, "signature_type"),
    ("POLYMARKET_PRIVATE_KEY", None, "private_key"),
    ("POLYMARKET_FUNDER", None, "funder"),
    ("POLYMARKET_USER_ADDRESS", "POLYMARKET_ADDRESS", "user_address"),
    ("POLYMARKET_EOA_ADDRESS", None, "eoa_address"),
    ("POLYMARKET_API_KEY", None, "builder_api_key"),              # builder relayer
    ("POLYMARKET_API_SECRET", None, "builder_api_secret"),
    ("POLYMARKET_PASSPHRASE", None, "builder_passphrase"),
]

_CREDENTIAL_FIELDS_PM = {p[2] for p in _PM_CRED_ENV if p[2] != "signature_type"}
_CREDENTIAL_FIELDS_OE = {p[1] for p in _OE_CRED_ENV}


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
    _migrate_legacy_arbitrage_fields(raw)
    _inject_env_credentials(raw)
    _inject_env_proxy(raw)

    try:
        return msgspec.convert(raw, type=ArbConfig)
    except msgspec.ValidationError as e:
        raise ConfigError(f"schema mismatch in {path}: {e}") from e


def _migrate_legacy_arbitrage_fields(raw: dict) -> None:
    """兼容旧配置:`risk.share/max_leg_share/fx` → 顶层 `arbitrage` 默认值。

    新 `arbitrage` 段显式字段优先;旧字段只在新字段缺失时补齐。
    """
    risk = raw.get("risk") or {}
    if not isinstance(risk, dict):
        return
    arb = raw.setdefault("arbitrage", {})
    if not isinstance(arb, dict):
        return
    for key in ("share", "max_leg_share", "fx"):
        if key not in arb and risk.get(key) is not None:
            arb[key] = risk[key]


def _warn_credentials_in_json(raw: dict) -> None:
    """如果 `venues.{polymarket,orbitexch}` 里有凭证字段非空 → 发 ConfigWarning。"""
    venues = raw.get("venues") or {}
    pm = venues.get("polymarket") or {}
    oe = venues.get("orbitexch") or {}
    leaked = []
    for k in _CREDENTIAL_FIELDS_PM:
        if pm.get(k):
            leaked.append(f"venues.polymarket.{k}")
    for k in _CREDENTIAL_FIELDS_OE:
        if oe.get(k):
            leaked.append(f"venues.orbitexch.{k}")
    if leaked:
        warnings.warn(
            f"credentials found in config JSON (should be env-only): {', '.join(leaked)}; "
            "rotate exposed credentials and migrate to env vars (see configuration.md §9)",
            ConfigWarning,
            stacklevel=3,
        )


def _inject_env_credentials(raw: dict) -> None:
    """env 凭证覆盖 raw['venues'].{polymarket,orbitexch} 字段(就地)。

    env 缺失 → 不覆盖,保留 JSON 值(或 None);**不验证存在**(下游 client 构造时 raise)。
    """
    raw.setdefault("venues", {})
    raw["venues"].setdefault("polymarket", {})
    raw["venues"].setdefault("orbitexch", {})

    pm = raw["venues"]["polymarket"]
    for env_name, fallback, field in _PM_CRED_ENV:
        val = os.environ.get(env_name)
        if val is None and fallback is not None:
            val = os.environ.get(fallback)
        if val is not None:
            if field == "signature_type":
                val = int(val)
            pm[field] = val

    oe = raw["venues"]["orbitexch"]
    for env_name, field in _OE_CRED_ENV:
        val = os.environ.get(env_name)
        if val is not None:
            oe[field] = val


def _inject_env_proxy(raw: dict) -> None:
    """PM CLOB WS 使用 NT pyo3 client,需显式 proxy_url;不读系统代理会导致直连超时。"""
    raw.setdefault("venues", {})
    raw["venues"].setdefault("polymarket", {})

    pm = raw["venues"]["polymarket"]
    if pm.get("proxy_url"):
        return

    for env_name in ("POLYMARKET_PROXY_URL", "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        val = os.environ.get(env_name)
        if val:
            pm["proxy_url"] = val
            return
