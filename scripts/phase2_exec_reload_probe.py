"""Phase 2(OE 健康检查状态维度)最小实盘探针 —— **零下单**。

验证唯一未知点(execution `architecture.md §4.3` 安全闸注释原文):
  *reload 已登录的 OrbitExch execution 交易页* —— 登录态是否存活 / 是否重现**登录后弹窗**
  (弹层盖住页面会堵住 general WS 推 BALANCE+CURRENT_BETS,见 execution.py:_dismiss_post_login_popup)/
  `CURRENT_BETS` 是否在 reload 后重推。这正是 `health_check_exec_reload_enabled` 默认关所守的风险。

⚠️ **已知代码隐患(本探针要证实/证伪)**:`OrbitExchDataClient._reload_execution_page`(data.py)只裸
`exec_page.reload()`,**reload 后不重新关弹窗**。若 OE 每次页面加载都弹窗(非仅首次登录),Phase 2 的
reload 会把 CURRENT_BETS 自己堵死 —— 那么 `_reload_execution_page` 需补一次 `_dismiss_post_login_popup`。

做法(不接 NT TradingNode,直接驱动真实 Data/Exec client 方法,stub NT 组件):
  1. 真账户登录(共享 BrowserManager,user_data_dir 持久化 profile)→ 开 execution 页 + general WS
  2. 等初始 BALANCE/CURRENT_BETS 到齐,快照「reload 前」状态
  3. **人为 `leg_settled.arm(...)` 造一个未结腿(不提交任何订单)** → `has_any_unsettled()` 为真
  4. 调真实 `OrbitExchDataClient._run_health_check()`(Phase 2 闸开)→ 触发 `_reload_execution_page`
  5. 等 reload 后 WS 帧,快照「reload 后」状态,出观测报告

**真账户、真登录、零下单**。需 `--config <arb_config.json>` + 凭证 env(项目根 `.env`)。
**前置**:arb_config 里 `venues.orbitexch.health_check_exec_reload_enabled: true`(探针 dogfood #74 接线,
从 dispatcher 读闸;未开则拒跑并提示)。建议加 `--headed` 亲眼看弹窗/会话。

用法:
  python3 -m scripts.phase2_exec_reload_probe --config arb_config.json --headed [--settle-wait 8]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from nautilus_trader.adapters.orbitexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.orbitexch.data import OrbitExchDataClient
from nautilus_trader.adapters.orbitexch.execution import OrbitExchExecutionClient

from src.arbitrage.common.leg_settled import LegSettledRegistry
from src.arbitrage.config import load_arb_config
from src.arbitrage.config.dispatcher import to_orbitexch_data_client_config
from src.arbitrage.config.dispatcher import to_orbitexch_exec_client_config


async def _popup_visible(page) -> bool:
    """复用 execution.py:_dismiss_post_login_popup 的定位器判断登录后弹窗是否盖在页面上。"""
    try:
        btn = page.locator('xpath=//button[normalize-space()="OK"]')
        return await btn.is_visible(timeout=3000)
    except Exception:
        return False


async def run(args) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(_PROJECT_ROOT / ".env")
    except ImportError:
        pass

    cfg = load_arb_config(args.config)
    exec_cfg = to_orbitexch_exec_client_config(cfg)
    data_cfg = to_orbitexch_data_client_config(cfg)

    # dogfood #74:从 arb_config 经 dispatcher 读闸。未开 → 拒跑(避免误以为在验 Phase 2)。
    if not data_cfg.health_check_exec_reload_enabled:
        print(
            "✗ venues.orbitexch.health_check_exec_reload_enabled = False(经 dispatcher 读到)。\n"
            "  Phase 2 探针需先在 arb_config 里把它置 true 再跑。",
        )
        return 2

    headless = exec_cfg.headless and not args.headed
    bm = PlaywrightBrowserManager(
        browser_type=exec_cfg.browser_type,
        headless=headless,
        user_data_dir=exec_cfg.user_data_dir,
    )
    leg = LegSettledRegistry()
    clock = LiveClock()
    loop = asyncio.get_running_loop()

    exec_client = OrbitExchExecutionClient(
        loop=loop, browser_manager=bm,
        msgbus=MessageBus(trader_id=TraderId("PROBE-EXEC-000"), clock=clock),
        cache=TestComponentStubs.cache(), clock=clock,
        instrument_provider=InstrumentProvider(), config=exec_cfg, leg_settled=leg,
    )
    data_client = OrbitExchDataClient(
        loop=loop, browser_manager=bm,
        msgbus=MessageBus(trader_id=TraderId("PROBE-DATA-000"), clock=clock),
        cache=TestComponentStubs.cache(), clock=clock,
        instrument_provider=InstrumentProvider(), config=data_cfg, leg_settled=leg,
    )

    # ── 观测埋点:记录 BALANCE / CURRENT_BETS 帧到达时刻(reload 前后对比是否重推)──
    frames: list[tuple[float, str]] = []
    orig_acct = exec_client.generate_account_state
    orig_bets = exec_client._on_current_bets

    def _acct_spy(*a, **k):
        frames.append((time.time(), "BALANCE"))
        return orig_acct(*a, **k)

    def _bets_spy(bets):
        frames.append((time.time(), "CURRENT_BETS"))
        return orig_bets(bets)

    exec_client.generate_account_state = _acct_spy
    exec_client._on_current_bets = _bets_spy

    print(f"▶ 连接(headless={headless},真账户登录,零下单)…")
    try:
        await exec_client._connect()
    except Exception as e:  # noqa: BLE001
        print(f"✗ _connect 失败:{e!r}")
        await bm.close()
        return 1

    await asyncio.sleep(args.settle_wait)
    page = exec_client._page
    before_url = page.url if page else "<no page>"
    before_popup = await _popup_visible(page) if page else False
    before_frames = list(frames)
    print(f"\n── reload 前 ──\n  url={before_url}\n  弹窗可见={before_popup}\n  收到帧={[k for _, k in before_frames]}")

    if page is None or "/customer/" not in (before_url or ""):
        print("✗ 未进入已登录状态(/customer/),无法继续 Phase 2 reload 验证。")
        await exec_client._disconnect()
        await bm.close()
        return 1

    # ── 人为造未结腿(零下单)→ has_any_unsettled() 为真 ──
    leg.arm("PROBE-PAIR", "probe-leg")
    assert leg.has_any_unsettled()
    print("\n▶ 已 arm 未结腿(无任何订单);调真实 _run_health_check() 触发 Phase 2 reload…")

    reload_ts = time.time()
    await data_client._run_health_check()
    await asyncio.sleep(args.settle_wait)

    after_url = page.url
    after_popup = await _popup_visible(page)
    post_reload_frames = [k for ts, k in frames if ts > reload_ts]
    print(f"\n── reload 后 ──\n  url={after_url}\n  弹窗可见={after_popup}\n  reload 后新帧={post_reload_frames}")

    # ── 判读 ──
    login_alive = "/customer/" in (after_url or "")
    bets_repushed = "CURRENT_BETS" in post_reload_frames
    print("\n══ Phase 2 观测结论 ══")
    print(f"  [{'✓' if login_alive else '✗'}] 登录态存活(reload 后仍 /customer/)")
    print(f"  [{'⚠️' if after_popup else '✓'}] 登录后弹窗{'重现(会堵 WS — _reload_execution_page 需补 dismiss)' if after_popup else '未重现'}")
    print(f"  [{'✓' if bets_repushed else '✗'}] CURRENT_BETS reload 后重推")
    verdict_ok = login_alive and not after_popup and bets_repushed
    print(f"\n  → Phase 2 reload {'安全,可考虑默认开闸' if verdict_ok else '有风险,保持默认关 / 先修隐患'}")

    await exec_client._disconnect()
    await bm.close()
    return 0 if verdict_ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OE Phase 2 execution-page reload probe (zero-order)")
    p.add_argument("--config", required=True, help="path to arb_config.json")
    p.add_argument("--headed", action="store_true", help="强制可见浏览器(亲眼看弹窗/会话)")
    p.add_argument("--settle-wait", type=float, default=8.0, help="connect / reload 后等 WS 帧的秒数")
    args = p.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
