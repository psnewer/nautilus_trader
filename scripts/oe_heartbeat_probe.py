"""OE SockJS 心跳周期探针 —— **零下单**(#105 待 live 项:定 exec 页存活 idle_timeout)。

目的:确认 OrbitExch execution 页 general WS(SockJS)**服务端心跳帧 `'h'`** 的实际周期,
用来给 #105 的"venue 死活 = reconcile 成败、idle 静默触发探测"里的 `idle_timeout` 定阈值
(被动存活锚 —— OE 不能主动 ping,只能靠收到的心跳帧刷 `_last_frame_ns`,见 execution `§4.3bis(4)`)。

做法(不接 NT TradingNode,直接驱动真实 OrbitExchExecutionClient,stub NT 组件):
  1. 真账户登录(共享 BrowserManager,user_data_dir 持久化 profile)→ 开 execution 页 + general WS
  2. 钩住 WS handler 的 `_on_frame_received`,记录每个 `'h'`(心跳)/ `'o'`(open)/ `a[...]`(业务)帧到达时刻
  3. 静默观测 `--observe-sec`(默认 180s,期间零操作)
  4. 出报告:各 ws_type 的心跳帧间隔(count / min / median / max),据此定 idle_timeout

**真账户、真登录、零下单、零 reload**。需 `--config <arb_config.json>` + 凭证 env(项目根 `.env`)。
建议 `--headed` 亲眼看页面。

用法:
  python3 -m scripts.oe_heartbeat_probe --config arb_config.json --headed [--observe-sec 180]
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
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
from nautilus_trader.adapters.orbitexch.execution import OrbitExchExecutionClient

from src.arbitrage.common.leg_settled import LegSettledRegistry
from src.arbitrage.config import load_arb_config
from src.arbitrage.config.dispatcher import to_orbitexch_exec_client_config


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
    exec_cfg = to_orbitexch_exec_client_config(cfg)

    headless = exec_cfg.headless and not args.headed
    bm = PlaywrightBrowserManager(
        browser_type=exec_cfg.browser_type,
        headless=headless,
        user_data_dir=exec_cfg.user_data_dir,
    )
    clock = LiveClock()
    loop = asyncio.get_running_loop()

    exec_client = OrbitExchExecutionClient(
        loop=loop, browser_manager=bm,
        msgbus=MessageBus(trader_id=TraderId("PROBE-HB-000"), clock=clock),
        cache=TestComponentStubs.cache(), clock=clock,
        instrument_provider=InstrumentProvider(), config=exec_cfg,
        leg_settled=LegSettledRegistry(),
    )

    # ── 观测埋点:记录每帧 (ts, ws_type, kind) ──
    frames: list[tuple[float, str, str]] = []

    print(f"▶ 连接(headless={headless},真账户登录,零下单)…")
    try:
        await exec_client._connect()
    except Exception as e:  # noqa: BLE001
        print(f"✗ _connect 失败:{e!r}")
        await bm.close()
        return 1

    handler = exec_client._ws_handler
    if handler is None:
        print("✗ exec WS handler 未初始化(_connect 未真接线 / 非 live)")
        await exec_client._disconnect()
        await bm.close()
        return 1

    # 钩住 handler 的 _on_frame_received(lambda 在调用时按属性查找,monkeypatch 生效)
    _orig_recv = handler._on_frame_received

    def _spy(ws_type: str, data: str):
        frames.append((time.time(), ws_type, _classify(data)))
        return _orig_recv(ws_type, data)

    handler._on_frame_received = _spy

    page = exec_client._page
    url = page.url if page else "<no page>"
    print(f"  url={url}")
    if page is None or "/customer/" not in (url or ""):
        print("✗ 未进入已登录状态(/customer/),无法观测 execution 页 general WS。")
        await exec_client._disconnect()
        await bm.close()
        return 1

    print(f"\n▶ 静默观测 {args.observe_sec}s(零操作,采心跳帧)… Ctrl-C 可提前结束")
    try:
        await asyncio.sleep(args.observe_sec)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("  (提前结束)")

    handler._on_frame_received = _orig_recv  # 摘钩

    # ── 报告:按 (ws_type, kind) 统计心跳间隔 ──
    print("\n══ 观测结论 ══")
    hb_by_ws: dict[str, list[float]] = {}
    for ts, ws_type, kind in frames:
        if kind == "heartbeat":
            hb_by_ws.setdefault(ws_type, []).append(ts)

    all_kinds: dict[tuple[str, str], int] = {}
    for _, ws_type, kind in frames:
        all_kinds[(ws_type, kind)] = all_kinds.get((ws_type, kind), 0) + 1
    print("  帧统计(ws_type, kind → count):")
    for (ws_type, kind), n in sorted(all_kinds.items()):
        print(f"    {ws_type:10s} {kind:10s} {n}")

    if not hb_by_ws:
        print("\n  ⚠️ 观测期内**未捕获心跳帧 'h'** —— 可能:观测时间短于心跳周期 / 该 WS 无 SockJS 心跳 /")
        print("     业务帧足够频繁未触发服务端心跳。建议加长 --observe-sec 或核对 ws_type。")
        ok = False
    else:
        print("\n  心跳间隔(ws_type → count / min / median / max,单位秒):")
        ok = True
        for ws_type, ts_list in hb_by_ws.items():
            ts_list.sort()
            gaps = [b - a for a, b in zip(ts_list, ts_list[1:])]
            if gaps:
                print(
                    f"    {ws_type:10s} n_gap={len(gaps):3d}  "
                    f"min={min(gaps):.1f}  median={statistics.median(gaps):.1f}  max={max(gaps):.1f}",
                )
            else:
                print(f"    {ws_type:10s} 仅 1 个心跳帧,无法算间隔(加长观测)")
        print("\n  → 用 median 心跳周期 + 余量定 idle_timeout(#105 暂定 300s;若 median≈25s 可保 300s 或收紧)。")

    await exec_client._disconnect()
    await bm.close()
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OE SockJS heartbeat-period probe (zero-order)")
    p.add_argument("--config", required=True, help="path to arb_config.json")
    p.add_argument("--headed", action="store_true", help="强制可见浏览器(亲眼看页面)")
    p.add_argument("--observe-sec", type=float, default=180.0, help="静默观测秒数(默认 180,建议 ≥ 数个心跳周期)")
    args = p.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
