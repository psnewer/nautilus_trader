"""
OrbitExch 浏览器辅助函数——登录、导航、弹窗处理。

2026-07-03: 从 execution.py 抽取,供 data factory 的 json_fetcher 复用。
"""

from __future__ import annotations


async def oe_login(page, config) -> None:
    """登录 OE 并等待 `/customer/` URL。

    流程:导航 → 填表 → 点按钮 → 等 URL 变化 → 关弹窗。
    已登录(URL 已是 /customer/)则跳过填表直接关弹窗。
    """
    url = getattr(page, "url", "") or ""
    # 如果已在 /customer/ 页面,说明 session 已登录
    if "/customer/" in url:
        await _dismiss_post_login_popup(page)
        return

    await page.goto(config.base_url, wait_until="domcontentloaded", timeout=config.page_timeout)
    url = getattr(page, "url", "") or ""
    if "/customer/" in url:
        # user_data_dir 恢复了 session,无需再登录
        await _dismiss_post_login_popup(page)
        return

    # 需要登录
    await page.wait_for_selector('input[name="username"]', timeout=10000)
    await page.fill('input[name="username"]', config.username)
    await page.fill('input[name="password"]', config.password)
    await page.click('button[type="submit"]:has-text("Log In")')
    await page.wait_for_url("**/customer/**", timeout=15000)
    await _dismiss_post_login_popup(page)


async def _dismiss_post_login_popup(page) -> None:
    """关登录后弹窗:等待弹窗出现 → 点主页面区域关闭。

    无弹窗/出错都不致命(吞掉)。
    """
    try:
        popup = page.locator('div[class*="_postLoginPopup_"]').first
        if callable(popup):
            popup = popup()
        await popup.wait_for(state="visible", timeout=5000)
        await page.mouse.click(24, 160)
    except Exception:
        pass  # 无弹窗或超时,不阻断


def oe_is_customer_url(url: str) -> bool:
    """判断 URL 是否为已登录的 /customer/ 页面。"""
    return "/customer/" in (url or "")


async def oe_csrf_token(page) -> str:
    """从 page context 读取 CSRF-TOKEN cookie。

    使用 Playwright API 而非 document.cookie,能读取 HttpOnly cookie。
    统一 OE 所有 API 调用的 CSRF token 获取方式。
    """
    context_fn = getattr(page, "context", None)
    context = context_fn() if callable(context_fn) else context_fn
    if context is None:
        return ""
    cookies_fn = getattr(context, "cookies", None)
    if cookies_fn is None:
        return ""
    try:
        cookies = await cookies_fn()
    except TypeError:
        # 某些 Playwright 版本需要传空列表
        cookies = await cookies_fn([])
    except Exception:
        return ""
    for cookie in cookies or []:
        if cookie.get("name") == "CSRF-TOKEN":
            return str(cookie.get("value") or "")
    return ""


async def oe_fetch_json(
    page,
    url: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    timeout_ms: int = 30000,
) -> dict:
    """在 OE 页面 context 中执行 POST fetch。

    统一的 fetch 函数,自动处理 CSRF token。
    """
    csrf_token = await oe_csrf_token(page)
    return await page.evaluate(
        """async ({url, params, body, timeoutMs, csrfToken}) => {
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
                        'x-csrf-token': csrfToken || '',
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
            "csrfToken": csrf_token,
        },
    )
