"""SharpExch 手动下单/撤单请求捕获器。

只做三件事:
1. 用 .env 凭证登录 SE headed browser。
2. 尝试关闭登录后弹窗。
3. 监听用户手动触发的 `/customer/api/placeBets` / `/customer/api/cancelBets` 请求与响应。

脚本本身不调用 placeBets/cancelBets。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from nautilus_trader.adapters.sharpexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.sharpexch.web import se_dismiss_post_login_popup
from nautilus_trader.adapters.sharpexch.web import se_login

from src.arbitrage.config import load_arb_config
from src.arbitrage.config.dispatcher import to_sharpexch_data_client_config


_CAPTURE_PATHS = ("/customer/api/placeBets", "/customer/api/cancelBets")


async def run(args) -> int:
    _load_dotenv()
    cfg = load_arb_config(args.config)
    se_cfg = to_sharpexch_data_client_config(cfg)
    if not se_cfg.username or not se_cfg.password:
        print("missing SHARPEXCH_USERNAME / SHARPEXCH_PASSWORD", flush=True)
        return 2

    browser = PlaywrightBrowserManager(
        browser_type=se_cfg.browser_type,
        headless=False,
        user_data_dir=se_cfg.user_data_dir,
    )
    captures: list[dict[str, Any]] = []

    await browser.start()
    page = await browser.create_page("se-request-capture")
    page.set_default_timeout(se_cfg.page_timeout)

    async def _on_response(response):
        url = response.url
        if not any(path in url for path in _CAPTURE_PATHS):
            return
        request = response.request
        record: dict[str, Any] = {
            "ts": time.time(),
            "url": url,
            "method": request.method,
            "request_headers": _sanitize_headers(request.headers),
            "request_body": _parse_json_or_text(request.post_data),
            "status": response.status,
            "response_headers": _sanitize_headers(response.headers),
            "response_body": None,
        }
        try:
            record["response_body"] = _parse_json_or_text(await response.text())
        except Exception as exc:  # noqa: BLE001
            record["response_body_error"] = repr(exc)
        captures.append(record)
        _print_capture(record)
        if args.write_file:
            Path(args.write_file).write_text(
                json.dumps(_sanitize(captures), ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )

    page.on("response", lambda response: asyncio.create_task(_on_response(response)))

    print("▶ SE request capture: login...", flush=True)
    await se_login(page, se_cfg)
    dismissed = await se_dismiss_post_login_popup(page, timeout_ms=1500)
    print(f"▶ login ready; popup dismissed={dismissed}", flush=True)

    if args.url:
        print(f"▶ navigate: {args.url}", flush=True)
        await page.goto(args.url, wait_until="domcontentloaded", timeout=se_cfg.page_timeout)
        dismissed = await se_dismiss_post_login_popup(page, timeout_ms=1500)
        print(f"▶ popup dismissed={dismissed}", flush=True)

    print("▶ READY: 请在打开的浏览器里手动下单/撤单；我会捕获 placeBets/cancelBets。Ctrl-C 结束。", flush=True)
    try:
        while True:
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        raise
    except KeyboardInterrupt:
        return 0
    finally:
        if args.write_file:
            Path(args.write_file).write_text(
                json.dumps(_sanitize(captures), ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        try:
            await browser.close()
        except Exception as exc:  # noqa: BLE001
            print(f"browser close ignored: {exc!r}", flush=True)


def _print_capture(record: dict[str, Any]) -> None:
    print("\n=== SE REQUEST CAPTURED ===", flush=True)
    print(json.dumps(_sanitize(record), ensure_ascii=False, indent=2, sort_keys=True), flush=True)


def _parse_json_or_text(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except Exception:  # noqa: BLE001
        return value


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    sensitive = ("cookie", "authorization", "x-csrf-token", "token")
    return {
        key: ("<redacted>" if any(item in key.lower() for item in sensitive) else value)
        for key, value in dict(headers or {}).items()
    }


def _sanitize(value: Any) -> Any:
    sensitive = ("password", "username", "token", "session", "cookie", "authorization", "auth", "csrf")
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_str = str(key)
            if any(token in key_str.lower() for token in sensitive):
                out[key_str] = "<redacted>"
            else:
                out[key_str] = _sanitize(item)
        return out
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(_PROJECT_ROOT / ".env")
    except ImportError:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture manual SharpExch place/cancel requests")
    parser.add_argument("--config", required=True)
    parser.add_argument("--url", default="https://portal.sharpxch.com/customer/sport/2/competition/12597512")
    parser.add_argument("--write-file", default="/tmp/se_request_capture.json")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
