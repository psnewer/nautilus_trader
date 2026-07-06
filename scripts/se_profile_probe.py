"""SE profile API probe - 捕获登录后的 profile 请求,分析 balance 数据来源。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from nautilus_trader.adapters.sharpexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.sharpexch.web import se_login

from src.arbitrage.config import load_arb_config
from src.arbitrage.config.dispatcher import to_sharpexch_data_client_config


async def run(config_path: str) -> None:
    """登录 SE 并捕获所有网络请求,特别是 profile/balance 相关的。"""

    def _load_dotenv() -> None:
        try:
            from dotenv import load_dotenv
            load_dotenv(_PROJECT_ROOT / ".env")
        except ImportError:
            pass

    _load_dotenv()
    cfg = load_arb_config(config_path)
    se_cfg = to_sharpexch_data_client_config(cfg)

    if not se_cfg.username or not se_cfg.password:
        print("✗ 缺 SHARPEXCH_USERNAME / SHARPEXCH_PASSWORD")
        return

    browser = PlaywrightBrowserManager(
        browser_type=se_cfg.browser_type,
        headless=False,  # 强制可见
        user_data_dir="/tmp/se-profile-probe",  # 使用独立 profile 避免冲突
    )

    # 收集的请求
    profile_requests = []
    balance_requests = []
    all_api_requests = []

    print("▶ SE profile probe start")
    print(f"  login_url={se_cfg.login_url}")

    try:
        await browser.start()
        page = await browser.create_page("se-profile-probe")
        page.set_default_timeout(se_cfg.page_timeout)

        # 监听所有网络请求
        def on_request(request):
            url = request.url
            # 只关注 API 请求
            if "/api/" in url or "backoffice" in url or "profile" in url.lower():
                req_info = {
                    "url": url,
                    "method": request.method,
                    "headers": dict(request.headers),
                }
                # 捕获 POST body
                if request.method == "POST" and "token" in url.lower():
                    try:
                        req_info["post_data"] = request.post_data
                    except:
                        pass
                all_api_requests.append(req_info)
                if "profile" in url.lower():
                    profile_requests.append(req_info)
                if "balance" in url.lower():
                    balance_requests.append(req_info)
                if "token" in url.lower():
                    print(f"\n🔑 TOKEN REQUEST: {request.method} {url}")
                    print(f"   Headers: {dict(request.headers)}")
                    try:
                        print(f"   Post data: {request.post_data}")
                    except:
                        pass

        def on_response(response):
            url = response.url
            if "profile" in url.lower() or "balance" in url.lower() or "backoffice" in url.lower():
                print(f"\n🔍 Response: {response.status} {url}")
                # 异步获取 body 需要特殊处理
                asyncio.create_task(_log_response_body(response))

        async def _log_response_body(response):
            try:
                body = await response.text()
                print(f"   Body: {body[:500]}...")
            except Exception as e:
                print(f"   Body error: {e}")

        page.on("request", on_request)
        page.on("response", on_response)

        # 登录
        print("\n── logging in ──")
        await se_login(page, se_cfg)
        print(f"✓ logged in, url={page.url}")

        # 等待一段时间让所有请求完成
        print("\n── waiting for API requests (15s) ──")
        await asyncio.sleep(15)

        # 打印结果
        print("\n\n══════════════════════════════════════")
        print("PROFILE REQUESTS:")
        print("══════════════════════════════════════")
        for req in profile_requests:
            print(f"\n  URL: {req['url']}")
            print(f"  Method: {req['method']}")
            print(f"  Headers: {json.dumps(req['headers'], indent=4)}")

        print("\n\n══════════════════════════════════════")
        print("BALANCE REQUESTS:")
        print("══════════════════════════════════════")
        for req in balance_requests:
            print(f"\n  URL: {req['url']}")
            print(f"  Method: {req['method']}")

        print("\n\n══════════════════════════════════════")
        print(f"ALL API REQUESTS ({len(all_api_requests)}):")
        print("══════════════════════════════════════")
        for req in all_api_requests[:30]:
            print(f"  {req['method']:6} {req['url']}")

        print("\n\n按 Ctrl+C 退出,或等待 60 秒...")
        await asyncio.sleep(60)

    except KeyboardInterrupt:
        print("\n中断")
    finally:
        await browser.close()


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "arb_config.json"
    asyncio.run(run(config_path))
