"""OE 赔率(prices)WS 空闲心跳探针 —— **零下单 / 零 reload**(#109 待决项)。

目的:确认 **competition 页 prices WS(`multiple-market-prices`,SockJS)在空闲盘口是否发服务端心跳帧 `'h'`**。
#109 把 OE competition 页存活做成"被动盯帧 gap → disconnect/reload";但被动判活**依赖对端周期发帧**。
execution `§4.3bis(4)` 的 2026-06-13 probe(那次测的是 exec 页 general WS,median ≈35s)顺带记了
"prices WS 因常推无心跳" —— 但那是**活跃盘口**的观察。本探针专门挑**空闲盘口**长观测,定论:
prices WS 空闲时**到底发不发 `'h'`**。

- 发 → #109 被动判活成立(保留帧-gap),只是阈值要 > 心跳周期。
- 不发 → #109 在空闲盘口会**误判 dead、误 reload**,judging 须改(close 事件即时 reload + 放宽/取消帧-gap)。

做法(不接 NT TradingNode,直接驱动真实 OrbitExchDataClient,stub NT 组件):
  1. 真账户(共享 BrowserManager,user_data_dir 持久化 profile)→ `bm.start()`(**不跑 instrument 发现**)
  2. `_open_or_reload_competition_page(sport_id, competition_id)` 开一个 competition 页 + prices WS
  3. **拆掉该页的 liveness reload**(清 on_disconnect + 停内部存活 timer)→ 纯观测、不 reload
  4. 钩 handler `_on_frame_received` 记录每帧 (ts, ws_type, kind);静默观测 `--observe-sec`
  5. 出报告:prices ws_type 的 `'h'` 心跳计数 / 间隔;**重点看挑的空闲盘口有没有 'h'**

**真账户、零下单、零 reload**。需 `--config <arb_config.json>` + 凭证 env(项目根 `.env`)+ 一个**空闲** competition。
建议挑赛前/休赛的安静盘口 + `--observe-sec 600`(覆盖多个心跳周期才能下"无心跳"的结论)+ `--headed`。

用法:
  python3 -m scripts.oe_heartbeat_probe --config arb_config.json \
      --sport-id <sport_id> --competition-id <competition_id> --headed --observe-sec 600
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from contextlib import suppress
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

from src.arbitrage.config import load_arb_config
from src.arbitrage.config.dispatcher import to_orbitexch_data_client_config


def _classify(data: str) -> str:
    """SockJS 帧分型(对齐 websocket_handler._on_frame_received)。"""
    if data == "h":
        return "heartbeat"
    if data == "o":
        return "open"
    if data.startswith("a"):
        return "data"
    return "other"


async def run(args) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(_PROJECT_ROOT / ".env")
    except ImportError:
        pass

    cfg = load_arb_config(args.config)
    data_cfg = to_orbitexch_data_client_config(cfg)

    headless = getattr(data_cfg, "headless", False) and not args.headed
    bm = PlaywrightBrowserManager(
        browser_type=getattr(data_cfg, "browser_type", "chromium"),
        headless=headless,
        user_data_dir=getattr(data_cfg, "user_data_dir", None),
    )
    clock = LiveClock()
    loop = asyncio.get_running_loop()

    dc = OrbitExchDataClient(
        loop=loop, browser_manager=bm,
        msgbus=MessageBus(trader_id=TraderId("PROBE-HB-000"), clock=clock),
        cache=TestComponentStubs.cache(), clock=clock,
        instrument_provider=InstrumentProvider(), config=data_cfg,
    )

    frames: list[tuple[float, str, str]] = []
    page_key = f"{args.sport_id}_{args.competition_id}"

    print(f"▶ 启动浏览器(headless={headless},真账户,零下单/零发现)…")
    try:
        await bm.start()
        await dc._open_or_reload_competition_page(page_key, args.sport_id, args.competition_id)
    except Exception as e:  # noqa: BLE001
        print(f"✗ 开 competition 页失败:{e!r}")
        with suppress(Exception):
            await bm.close()
        return 1

    handler = dc._comp_handlers.get(page_key)
    if handler is None:
        print(f"✗ competition 页 {page_key} handler 未建立")
        with suppress(Exception):
            await bm.close()
        return 1

    # 纯观测:拆掉 liveness reload(清 on_disconnect 回调 + 停内部存活 timer),避免观测期误 reload 污染
    handler._disconnect_callbacks.clear()
    with suppress(Exception):
        clock.cancel_timer(handler._liveness_name)

    # 钩 handler 的 _on_frame_received(lambda 调用时按属性查找 → monkeypatch 生效)
    _orig_recv = handler._on_frame_received

    def _spy(ws_type: str, data: str):
        frames.append((time.time(), ws_type, _classify(data)))
        return _orig_recv(ws_type, data)

    handler._on_frame_received = _spy

    page = dc._comp_pages.get(page_key)
    print(f"  page_key={page_key}  url={page.url if page else '<no page>'}  ws={dc._websocket_summary(handler)}")

    print(f"\n▶ 静默观测 {args.observe_sec}s(零操作)… 挑的盘口越安静、结论越硬。Ctrl-C 可提前结束")
    try:
        await asyncio.sleep(args.observe_sec)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("  (提前结束)")

    handler._on_frame_received = _orig_recv  # 摘钩

    # ── 报告 ──
    print("\n══ 观测结论 ══")
    all_kinds: dict[tuple[str, str], int] = {}
    for _, ws_type, kind in frames:
        all_kinds[(ws_type, kind)] = all_kinds.get((ws_type, kind), 0) + 1
    print("  帧统计(ws_type, kind → count):")
    for (ws_type, kind), n in sorted(all_kinds.items()):
        print(f"    {ws_type:10s} {kind:10s} {n}")

    prices_hb = sorted(ts for ts, ws_type, kind in frames if ws_type == "prices" and kind == "heartbeat")
    prices_any = [ts for ts, ws_type, _ in frames if ws_type == "prices"]
    print("")
    if not prices_any:
        print("  ⚠️ 观测期内 prices WS **零帧** —— 该页可能未建 prices WS / page_key 不对 / 该 competition 无行情。")
        print("     核对 sport_id/competition_id;或换一个有盘口的 competition。")
        return 1
    if prices_hb:
        gaps = [b - a for a, b in zip(prices_hb, prices_hb[1:])]
        print(f"  ✅ prices WS **有心跳 'h'**:count={len(prices_hb)}", end="")
        if gaps:
            print(f"  间隔 min={min(gaps):.1f} median={statistics.median(gaps):.1f} max={max(gaps):.1f}s")
        else:
            print("(仅 1 个,加长观测算间隔)")
        print("  → #109 被动判活成立;idle_timeout 取 > median 心跳周期 + 余量即可。")
        return 0
    print(f"  ❌ prices WS **未见心跳 'h'**(只见 {len(prices_any)} 个非心跳帧)。")
    print("     若该盘口确实安静(无赔率推送)却仍无 'h' → 证实 prices WS 不发心跳:")
    print("     #109 的帧-gap 判活在空闲盘口会误判 dead,judging 须改(close 即时 reload + 放宽/取消帧-gap)。")
    print("     建议:加长 --observe-sec、确认盘口真安静(无赔率帧)再下结论。")
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OE prices-WS idle-heartbeat probe (#109, zero-order/zero-reload)")
    p.add_argument("--config", required=True, help="path to arb_config.json")
    p.add_argument("--sport-id", required=True, help="competition 的 sport_id(event_type_id)")
    p.add_argument("--competition-id", required=True, help="competition_id")
    p.add_argument("--headed", action="store_true", help="强制可见浏览器(亲眼看页面)")
    p.add_argument("--observe-sec", type=float, default=600.0, help="静默观测秒数(默认 600,空闲盘口需覆盖多个心跳周期)")
    args = p.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
