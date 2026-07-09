"""SharpExch Playwright 页面事实 helper。

SE 登录入口在 `sharpxch.com/player/`,真实 customer app 运行在
`portal.sharpxch.com/customer` iframe 中。所有 customer API fetch 都应在该 iframe
context 内执行,否则会被浏览器按跨 origin 请求拒绝。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from dataclasses import field
from typing import Any

_log = logging.getLogger(__name__)


@dataclass
class SharpExchLoginState:
    """SE browser context 级登录状态。"""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    authenticated: bool = False


def se_is_customer_url(url: str) -> bool:
    return "/customer" in (url or "")


def se_customer_frame(page) -> Any | None:
    for frame in getattr(page, "frames", []) or []:
        if se_is_customer_url(getattr(frame, "url", "")):
            return frame
    return None


def se_customer_context(page):
    return se_customer_frame(page) or page


async def se_wait_for_customer_frame(page, *, timeout_ms: int) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if se_customer_frame(page) is not None:
            return
        await asyncio.sleep(0.2)
    raise TimeoutError("SE customer iframe did not appear")


async def se_login(page, config, browser_lock=None, login_state: SharpExchLoginState | None = None) -> None:
    """登录 SE 并等待 customer iframe 出现。

    不使用 `networkidle`:customer app 会长期维持 websocket,该条件不稳定。

    已登录时跳过导航:如果当前页已在 customer app 中且登录表单不可见,直接返回,
    避免重复 goto 触发 Cloudflare。若 customer iframe 与登录表单并存,以登录表单为准,
    必须重新提交凭据。

    browser_lock:可选的 asyncio.Lock,用于串行化多个 page 的登录操作。
    NT 启动期 Data/Exec 并发 connect,同一 browser context 内并发登录会触发
    Cloudflare 验证。通过 browser_lock 串行化登录,第一个 page 完成登录后,
    后续 page 可复用 context 内的 session cookies。
    """

    if login_state is not None:
        async with login_state.lock:
            await _se_login_impl(page, config, authenticated=login_state.authenticated)
            login_state.authenticated = True
    elif browser_lock is not None:
        async with browser_lock:
            await _se_login_impl(page, config)
    else:
        await _se_login_impl(page, config)


async def _se_login_impl(page, config, *, authenticated: bool = False) -> None:
    """se_login 的实际实现。"""

    if authenticated:
        if not (se_is_customer_url(getattr(page, "url", "") or "") or se_customer_frame(page) is not None):
            await page.goto(config.login_url, wait_until="domcontentloaded", timeout=config.page_timeout)
        if await _customer_app_available(page, timeout_ms=5000):
            await se_dismiss_post_login_popup(page, timeout_ms=2500)
            await _settle_customer_app(page)
            return

    # 快速路径:已在 customer app 中且没有登录表单,无需重新导航登录。
    # 若 iframe 和登录表单并存,说明页面处于半登录/过期状态,表单优先。
    current_url = getattr(page, "url", "") or ""
    if se_is_customer_url(current_url) or se_customer_frame(page) is not None:
        if await _login_form_visible(page, timeout_ms=1000):
            await _submit_login_form(page, config)
            await _wait_after_login(page)
            return
        await se_dismiss_post_login_popup(page, timeout_ms=2500)
        await _settle_customer_app(page)
        return

    await page.goto(config.login_url, wait_until="domcontentloaded", timeout=config.page_timeout)
    if await _login_form_visible(page, timeout_ms=5000):
        await _submit_login_form(page, config)
        await _wait_after_login(page)
        return

    if se_is_customer_url(getattr(page, "url", "")) or se_customer_frame(page) is not None:
        _log.info("SE login: already authenticated via session cookies")
        await se_dismiss_post_login_popup(page, timeout_ms=2500)
        await _settle_customer_app(page)
        return

    try:
        await se_wait_for_customer_frame(page, timeout_ms=5000)
        if await _login_form_visible(page, timeout_ms=1000):
            await _submit_login_form(page, config)
            await _wait_after_login(page)
            return
        await se_dismiss_post_login_popup(page, timeout_ms=2500)
        await _settle_customer_app(page)
        return
    except TimeoutError:
        pass

    # Re-check after customer frame wait - page state might have changed
    current_url_after = getattr(page, "url", "") or ""
    if se_is_customer_url(current_url_after) or se_customer_frame(page) is not None:
        if await _login_form_visible(page, timeout_ms=1000):
            await _submit_login_form(page, config)
            await _wait_after_login(page)
            return
        await se_dismiss_post_login_popup(page, timeout_ms=2500)
        await _settle_customer_app(page)
        return

    # Wait for page to stabilize - might be redirecting or loading
    await _settle_customer_app(page)

    # Final check before attempting login form
    if se_is_customer_url(getattr(page, "url", "")) or se_customer_frame(page) is not None:
        if await _login_form_visible(page, timeout_ms=1000):
            await _submit_login_form(page, config)
            await _wait_after_login(page)
            return
        await se_dismiss_post_login_popup(page, timeout_ms=2500)
        await _settle_customer_app(page)
        return

    # If login form is visible, submit it; otherwise page might be stuck on challenge
    if await _login_form_visible(page, timeout_ms=3000):
        await _submit_login_form(page, config)
        await _wait_after_login(page)
        return

    # Page is neither login form nor customer app - might be Cloudflare challenge or error
    # Wait a bit longer for manual challenge resolution, then re-check
    await asyncio.sleep(5.0)
    if se_is_customer_url(getattr(page, "url", "")) or se_customer_frame(page) is not None:
        await se_dismiss_post_login_popup(page, timeout_ms=2500)
        await _settle_customer_app(page)
        return

    raise TimeoutError(
        f"SE login stuck: not on customer URL ({getattr(page, 'url', '')!r}) "
        "and login form not visible - possible Cloudflare challenge"
    )


async def _login_form_visible(page, *, timeout_ms: int) -> bool:
    try:
        await page.wait_for_selector(
            'input[name="username"], input[type="text"]',
            state="visible",
            timeout=timeout_ms,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


async def _customer_app_available(page, *, timeout_ms: int) -> bool:
    if se_is_customer_url(getattr(page, "url", "") or "") or se_customer_frame(page) is not None:
        return True
    try:
        await se_wait_for_customer_frame(page, timeout_ms=timeout_ms)
        return True
    except TimeoutError:
        return False


async def _submit_login_form(page, config) -> None:
    await page.wait_for_selector('input[name="username"], input[type="text"]', timeout=15000)
    _log.info("SE login: submitting credentials via form")
    await _fill_first(page, ['input[name="username"]', 'input[type="text"]'], config.username)
    await _fill_first(page, ['input[name="password"]', 'input[type="password"]'], config.password)
    await _click_first(
        page,
        [
            'button[type="submit"]:has-text("Log In")',
            'button:has-text("Log In")',
            'button:has-text("Login")',
            'input[type="submit"]',
        ],
    )


async def _wait_after_login(page) -> None:
    try:
        await se_wait_for_customer_frame(page, timeout_ms=10000)
    except Exception:
        await page.wait_for_url("**/customer**", timeout=10000)
    await se_dismiss_post_login_popup(page, timeout_ms=2500)
    await _settle_customer_app(page)


async def se_dismiss_post_login_popup(page, logger=None, *, timeout_ms: int = 7000) -> bool:
    """关闭 SE 登录后弹窗。

    该弹窗可能挡住 customer app 初始化/接口请求。策略与 OE #89 一致:等容器出现后点
    主页面区域关闭;没有弹窗时静默继续。
    """

    for context in _popup_contexts(page):
        try:
            popup = context.locator('div[class*="_postLoginPopup_"]').first
            if callable(popup):
                popup = popup()
            await popup.wait_for(state="visible", timeout=timeout_ms)
            await _click_popup_backdrop(context, page)
            if logger is not None:
                logger.info("SharpExch post-login popup dismissed")
            return True
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    if logger is not None:
        debug = getattr(logger, "debug", None)
        if debug is not None:
            debug(f"SharpExch no post-login popup (error: {last_exc})")
    return False


def _popup_contexts(page) -> list:
    frame = se_customer_frame(page)
    contexts = []
    if frame is not None:
        contexts.append(frame)
    contexts.append(page)
    return contexts


async def _click_popup_backdrop(context, page) -> None:
    try:
        body = context.locator("body").first
        if callable(body):
            body = body()
        await body.click(position={"x": 24, "y": 160}, timeout=2500)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        await page.mouse.click(24, 160)
    except Exception:
        raise


async def se_fetch_json(
    context,
    url: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    timeout_ms: int = 30000,
) -> dict:
    return await context.evaluate(
        """async ({url, params, body, timeoutMs}) => {
            const qs = new URLSearchParams(params || {}).toString();
            let path = url;
            try {
                const parsed = new URL(url, window.location.origin);
                if (parsed.origin === window.location.origin) {
                    path = `${parsed.pathname}${parsed.search}`;
                }
            } catch (e) {}
            const finalUrl = qs ? `${path}${path.includes('?') ? '&' : '?'}${qs}` : path;
            const controller = new AbortController();
            const timeout = setTimeout(() => controller.abort(), timeoutMs);
            try {
                const res = await fetch(finalUrl, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'accept': 'application/json, text/plain, */*',
                        'content-type': 'application/json',
                        'x-device': 'DESKTOP',
                    },
                    body: JSON.stringify(body || {}),
                    signal: controller.signal,
                });
                const text = await res.text();
                let json = null;
                try { json = JSON.parse(text); } catch (e) {}
                return {ok: res.ok, status: res.status, text: text.slice(0, 500), json};
            } catch (e) {
                return {
                    ok: false,
                    status: 0,
                    text: `fetch_error:${e && e.name ? e.name : 'Error'}:${e && e.message ? e.message : String(e)}`,
                    json: null,
                };
            } finally {
                clearTimeout(timeout);
            }
        }""",
        {
            "url": url,
            "params": params or {},
            "body": body or {},
            "timeoutMs": max(1, int(timeout_ms)),
        },
    )


async def _settle_customer_app(page) -> None:
    """No-op: domcontentloaded + fetch retry logic handles app readiness."""
    pass


async def _fill_first(page, selectors: list[str], value: str) -> None:
    last_error: Exception | None = None
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            await locator.fill(value, timeout=5000)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("no selector candidates")


async def _click_first(page, selectors: list[str]) -> None:
    last_error: Exception | None = None
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            await locator.click(timeout=5000)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("no selector candidates")
