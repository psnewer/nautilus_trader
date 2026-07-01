"""SharpExch 独立可观测 probe —— 真登录 / 真 API / 真 WS,**零下单**。

目的:
  先把 SE 网站事实搞清楚(login / iframe / sport/details / prices WS / general WS /
  BALANCE / CURRENT_BETS schema),但不启动 TradingNode,不进入 Matching/Strategy/Risk,
  不提交或撤销任何订单。

用法:
  python3 -m scripts.se_probe --config arb_config.json --headed --wait 20
  python3 -m scripts.se_probe --config arb_config.json --headed --write-dir /tmp/se_probe

安全边界:
  - 默认只打印摘要,不保存完整业务帧。
  - `--write-dir` 只写脱敏样本,用于后续补 parser fixture。
  - 本脚本不调用 placeBets/cancelBets,也不构造 NT SubmitOrder。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from nautilus_trader.adapters.sharpexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.sharpexch.discovery_client import events_from_sport_details
from nautilus_trader.adapters.sharpexch.discovery_client import sport_details_request
from nautilus_trader.adapters.sharpexch.message_parser import SharpExchMessageParser
from nautilus_trader.adapters.sharpexch.web import se_customer_context
from nautilus_trader.adapters.sharpexch.web import se_fetch_json
from nautilus_trader.adapters.sharpexch.web import se_is_customer_url
from nautilus_trader.adapters.sharpexch.web import se_login
from nautilus_trader.adapters.sharpexch.websocket_handler import SharpExchWebSocketHandler

from src.arbitrage.config import load_arb_config
from src.arbitrage.config.dispatcher import to_se_discovery_config
from src.arbitrage.config.dispatcher import to_sharpexch_data_client_config


_SENSITIVE_KEYS = {
    "password",
    "username",
    "token",
    "session",
    "cookie",
    "authorization",
    "auth",
    "jwt",
    "csrf",
}
_MAX_SPORT_DETAILS_PAGES = 100


class _ProbeLog:
    def info(self, msg: str) -> None:
        print(msg)

    def debug(self, msg: str) -> None:
        return

    def error(self, msg: str) -> None:
        print(f"ERROR: {msg}")


async def run(args) -> int:
    _load_dotenv()
    cfg = load_arb_config(args.config)
    se_cfg = to_sharpexch_data_client_config(cfg)
    se_discovery = to_se_discovery_config(cfg)

    if not se_cfg.username or not se_cfg.password:
        print("✗ 缺 SHARPEXCH_USERNAME / SHARPEXCH_PASSWORD(env 注入后仍为空)")
        return 2

    headless = se_cfg.headless and not args.headed
    browser = PlaywrightBrowserManager(
        browser_type=se_cfg.browser_type,
        headless=headless,
        user_data_dir=se_cfg.user_data_dir,
    )
    parser = SharpExchMessageParser()
    samples = _ProbeSamples(limit=args.sample_limit)

    print(f"▶ SE probe start(headless={headless}, zero-order)")
    print(f"  login_url={se_cfg.login_url}")
    print(f"  base_url={se_cfg.base_url}")

    try:
        await browser.start()
        page = await browser.create_page("se-probe")
        page.set_default_timeout(se_cfg.page_timeout)

        ws = SharpExchWebSocketHandler(page, logger=_ProbeLog())
        ws.on_price_update(lambda msg: samples.record_price(msg, parser.parse_price_message(msg)))
        ws.on_order_update(lambda msg: samples.record_general(msg, parser.parse_general_frame(msg)))
        await ws.start()

        login = await _login(page, se_cfg)
        print("\n── login ──")
        print(f"  ok={login['ok']} url={login['url']}")
        print(f"  frames={login['frames']}")
        if not login["ok"]:
            await _write_probe_output(args.write_dir, samples, extra={"login": login})
            return 1

        discovery = await _fetch_sport_details(se_customer_context(page), se_cfg.base_url, se_discovery)
        print("\n── sport/details ──")
        print(f"  ok={discovery['ok']} requests={len(discovery['requests'])} events={len(discovery['events'])}")
        if discovery.get("error"):
            print(f"  error={discovery['error']}")
        for competition, count in _competition_counts(discovery["events"])[:8]:
            print(f"  competition {competition!r}: {count}")
        for event in discovery["events"][:5]:
            print(
                "  event "
                f"sport={event['sport']!r} competition={event['competition']!r} "
                f"{event['home_team']} vs {event['away_team']} "
                f"market_id={event['market_id']}",
            )

        if discovery["events"]:
            first = discovery["events"][0]
            url = _competition_url(se_cfg.base_url, first)
            print(f"\n▶ open competition page: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=se_cfg.page_timeout)

        await asyncio.sleep(args.wait)

        active_ws = ws.get_active_websockets()
        summary = samples.summary(active_ws)
        print("\n── websocket summary ──")
        print(f"  active={summary['active_websockets']}")
        print(f"  price_frames={summary['price_frames']} parsed_price={summary['parsed_price_frames']}")
        print(f"  general_frames={summary['general_frames']} parsed_general={summary['parsed_general_frames']}")
        print(f"  balance_frames={summary['balance_frames']} current_bets_frames={summary['current_bets_frames']}")

        await _write_probe_output(args.write_dir, samples, extra={"login": login, "discovery": discovery, "summary": summary})
        await ws.stop()
        return 0 if discovery["ok"] and (summary["active_websockets"] or summary["parsed_price_frames"]) else 1
    finally:
        await browser.close()


async def _login(page, cfg) -> dict[str, Any]:
    before = await _page_snapshot(page)
    try:
        await se_login(page, cfg)
    except Exception as exc:  # noqa: BLE001
        after = await _page_snapshot(page)
        return {"ok": False, "url": page.url, "frames": after["frames"], "before": before, "error": repr(exc)}
    after = await _page_snapshot(page)
    ok = se_is_customer_url(page.url or "") or any(se_is_customer_url(frame) for frame in after["frames"])
    return {"ok": ok, "url": page.url, "frames": after["frames"], "before": before}


async def _fetch_sport_details(context, base_url: str, discovery_config) -> dict[str, Any]:
    sports = list(getattr(discovery_config, "sports", []) or []) if discovery_config is not None else [None]
    requests = []
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    payload_samples: list[dict[str, Any]] = []

    for sport in sports or [None]:
        page = 0
        size = 60
        seen_market_ids: set[str] = set()
        while page < _MAX_SPORT_DETAILS_PAGES:
            req = sport_details_request(base_url, sport, page=page, size=size)
            requests.append(req)
            try:
                payload = await se_fetch_json(context, req.url, params=req.params, body=req.body)
                payload_samples.append(_sanitize(payload))
                if not payload.get("ok") or not isinstance(payload.get("json"), dict):
                    errors.append(f"{req.body.get('id')}: status={payload.get('status')} text={payload.get('text')!r}")
                    break
                content = _payload_markets(payload["json"])
                new_market_ids = {
                    str(market.get("marketId") or "")
                    for market in content
                    if isinstance(market, dict) and market.get("marketId") is not None
                } - seen_market_ids
                if not content or (page > 0 and not new_market_ids):
                    break
                seen_market_ids.update(new_market_ids)
                parsed = events_from_sport_details(payload["json"], target_competitions=req.target_competitions)
                events.extend(asdict(event) for event in parsed)
                if len(content) < size:
                    break
                page += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{req.body.get('id')}: {exc!r}")
                break

    return {
        "ok": not errors,
        "requests": [_sanitize(asdict(req)) for req in requests],
        "events": _sanitize(events),
        "payload_samples": payload_samples[:3],
        "error": "; ".join(errors),
    }


def _payload_markets(payload: dict) -> list[dict]:
    catalogue = payload.get("marketCatalogueList") or {}
    content = catalogue.get("content") if isinstance(catalogue, dict) else None
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    return []


def _competition_url(base_url: str, event: dict[str, Any]) -> str:
    return (
        f"{base_url.rstrip('/')}/customer/sport/"
        f"{event['sport_id']}/competition/{event['competition_id']}"
    )


def _competition_counts(events: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for event in events:
        competition = str(event.get("competition") or "")
        counts[competition] = counts.get(competition, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


async def _page_snapshot(page) -> dict[str, Any]:
    return {
        "url": page.url,
        "frames": [frame.url for frame in page.frames],
    }


class _ProbeSamples:
    def __init__(self, *, limit: int) -> None:
        self.limit = max(0, int(limit))
        self.price_raw: list[Any] = []
        self.price_parsed: list[Any] = []
        self.general_raw: list[Any] = []
        self.general_parsed: list[Any] = []

    def record_price(self, raw: Any, parsed: Any) -> None:
        self._append(self.price_raw, raw)
        self._append(self.price_parsed, parsed)

    def record_general(self, raw: Any, parsed: Any) -> None:
        self._append(self.general_raw, raw)
        self._append(self.general_parsed, parsed)

    def summary(self, active_ws: list[dict[str, str]]) -> dict[str, Any]:
        parsed_general = [item for item in self.general_parsed if isinstance(item, dict)]
        return {
            "active_websockets": active_ws,
            "price_frames": len(self.price_raw),
            "parsed_price_frames": len([item for item in self.price_parsed if item]),
            "general_frames": len(self.general_raw),
            "parsed_general_frames": len(parsed_general),
            "balance_frames": len([item for item in parsed_general if item.get("type") == "balance"]),
            "current_bets_frames": len([item for item in parsed_general if item.get("type") == "current_bets"]),
        }

    def redacted(self) -> dict[str, Any]:
        return _sanitize(
            {
                "price_raw": self.price_raw,
                "price_parsed": self.price_parsed,
                "general_raw": self.general_raw,
                "general_parsed": self.general_parsed,
            },
        )

    def _append(self, target: list[Any], value: Any) -> None:
        if self.limit <= 0 or len(target) >= self.limit:
            return
        target.append(value)


async def _write_probe_output(write_dir: str | None, samples: _ProbeSamples, *, extra: dict[str, Any]) -> None:
    if not write_dir:
        return
    path = Path(write_dir)
    path.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": int(time.time()),
        "extra": _sanitize(extra),
        "samples": samples.redacted(),
    }
    out = path / "se_probe_redacted.json"
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\n  wrote redacted probe output: {out}")


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_str = str(key)
            if any(token in key_str.lower() for token in _SENSITIVE_KEYS):
                sanitized[key_str] = "<redacted>"
            else:
                sanitized[key_str] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item) for item in value]
    return value


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(_PROJECT_ROOT / ".env")
    except ImportError:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SharpExch zero-order login/API/WS probe")
    parser.add_argument("--config", required=True, help="path to arb_config.json")
    parser.add_argument("--headed", action="store_true", help="强制可见浏览器")
    parser.add_argument("--wait", type=float, default=20.0, help="打开 competition 页后等待 WS 帧秒数")
    parser.add_argument("--write-dir", default=None, help="写入脱敏 probe 样本的目录")
    parser.add_argument("--sample-limit", type=int, default=3, help="每类帧最多保存几个脱敏样本")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
