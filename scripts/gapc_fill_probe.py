"""Gap C 真单验证探针(Tier 2:**会成交**,真金真持仓)。

验证 Tier 1 未覆盖的最后一块(refactor.md Gap C / [[gap_c_oe_exec_live_validated]]):
  真成交后 `CURRENT_BETS` 的 **matched 帧填充值**(`sizeMatched>0`/`averagePrice>0`)+
  `current_bets_to_fills` 算 delta + `generate_order_filled`(`last_px=averagePrice`,
  **`liquidity_side=MAKER` 假设**,见 execution.py:_on_current_bets)产出是否正确。

⚠️⚠️ **本探针会真成交、真金真持仓** ⚠️⚠️
  - **市价 BACK @ 1.01 最小注**:BACK@1.01 等于"以 ≥1.01 任意 back 赔率成交"→ 瞬间吃最优 lay 盘、
    在最优 back 赔率**立即成交**(TAKER)。**最大损失 = 注金(该 selection 输)**。
  - **探针只下单 + 报告 matched 帧,不撤单/不对冲**(用户选:仓手动处理)。成交后你**持有一笔真实
    BACK 仓**,需自行到 OE 页面平/持。
  - 两道闸:**默认 dry-run**(只打印将下的会成交单,`--confirm` 才真下);成交不可撤,故无撤单兜底,
    改为**显著持仓告警**。若产生未成交余量(部分成交的 working 部分),用 `gapc_place_cancel_probe.py
    --cleanup-only` 清。

用法:
  # 先 dry-run(不下单):
  python3 -m scripts.gapc_fill_probe --config arb_config.json --headed
  # 确认后真下单(会成交、真持仓):
  python3 -m scripts.gapc_fill_probe --config arb_config.json --headed --confirm --size 7
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from nautilus_trader.adapters.orbitexch.execution import OrbitExchExecutionClient
from nautilus_trader.adapters.orbitexch.execution import bet_order_progress

from src.arbitrage.common.leg_settled import LegSettledRegistry
from src.arbitrage.config import load_arb_config
from src.arbitrage.config.dispatcher import to_orbitexch_exec_client_config

# 复用 Tier 1 探针的发现 / 展示 / 最小注金(同一套真理源,避免漂移)
from scripts.gapc_place_cancel_probe import _MIN_OE_STAKE
from scripts.gapc_place_cancel_probe import _discover_one_instrument
from scripts.gapc_place_cancel_probe import _inst_display

_MARKETABLE_BACK_ODDS = 1.01  # BACK@1.01 = 市价吃盘,立即成交(TAKER)


async def run(args) -> int:
    if args.size < _MIN_OE_STAKE:
        print(f"✗ --size={args.size} 低于 OE 最小 stake {_MIN_OE_STAKE};请使用 --size {_MIN_OE_STAKE} 或更高")
        return 2

    try:
        from dotenv import load_dotenv
        load_dotenv(_PROJECT_ROOT / ".env")
    except ImportError:
        pass

    cfg = load_arb_config(args.config)
    exec_cfg = to_orbitexch_exec_client_config(cfg)

    print("▶ OE 发现(取一条真实 instrument)…")
    inst = await _discover_one_instrument(cfg)
    if inst is None:
        print("✗ 未发现任何 instrument;终止(检查 discovery 配置 / 该 competition 是否有进行中赛事)")
        return 1
    print("  选用 instrument:\n" + _inst_display(inst))

    from nautilus_trader.adapters.orbitexch.browser_manager import PlaywrightBrowserManager
    headless = exec_cfg.headless and not args.headed
    bm = PlaywrightBrowserManager(
        browser_type=exec_cfg.browser_type, headless=headless, user_data_dir=exec_cfg.user_data_dir,
    )
    clock = LiveClock()
    cache = TestComponentStubs.cache()
    cache.add_instrument(inst)
    exec_client = OrbitExchExecutionClient(
        loop=asyncio.get_running_loop(), browser_manager=bm,
        msgbus=MessageBus(trader_id=TraderId("PROBE-FILL-000"), clock=clock),
        cache=cache, clock=clock,
        instrument_provider=InstrumentProvider(),
        config=exec_cfg, leg_settled=LegSettledRegistry(),
    )

    # 观测埋点:venue_order_id + generate_order_filled 实参(验 last_px/liquidity/qty)+ CURRENT_BETS 帧
    captured: dict = {"voi": None, "fills": []}
    bets_frames: list[float] = []
    orig_accepted = exec_client.generate_order_accepted
    orig_filled = exec_client.generate_order_filled
    orig_bets = exec_client._on_current_bets

    def _accepted_spy(*, venue_order_id, **k):
        captured["voi"] = venue_order_id
        return orig_accepted(venue_order_id=venue_order_id, **k)

    def _filled_spy(*, last_qty, last_px, liquidity_side, order_side, **k):
        captured["fills"].append({
            "last_qty": str(last_qty), "last_px": str(last_px),
            "liquidity": str(liquidity_side), "side": str(order_side),
        })
        return orig_filled(last_qty=last_qty, last_px=last_px,
                           liquidity_side=liquidity_side, order_side=order_side, **k)

    def _bets_spy(bets):
        bets_frames.append(time.time())
        return orig_bets(bets)

    exec_client.generate_order_accepted = _accepted_spy
    exec_client.generate_order_filled = _filled_spy
    exec_client._on_current_bets = _bets_spy

    print(f"▶ 连接(headless={headless},真登录)…")
    try:
        await exec_client._connect()
    except Exception as e:  # noqa: BLE001
        print(f"✗ _connect 失败:{e!r}")
        await bm.close()
        return 1

    # 构单:市价 BACK@1.01(会成交)
    factory = OrderFactory(trader_id=TraderId("PROBE-FILL-000"), strategy_id=StrategyId("PROBE-S"), clock=clock)
    order = factory.limit(
        inst.id, OrderSide.BUY, inst.make_qty(args.size), inst.make_price(_MARKETABLE_BACK_ODDS),
    )
    cache.add_order(order)
    print(
        f"\n── 将要下的单(⚠️ 会成交、真持仓)──\n{_inst_display(inst)}\n  side=BUY(BACK)\n"
        f"  odds={_MARKETABLE_BACK_ODDS}(市价,吃最优 lay 盘立即成交)\n  size={args.size}\n"
        f"  最大损失 = {args.size} GBP(该 selection 输)\n  client_order_id={order.client_order_id}",
    )

    if not args.confirm:
        print(
            "\n⏸  dry-run(未加 --confirm):不下单。"
            "确认 side=BUY(BACK)、odds=1.01(市价会成交)、size、最大损可承受后,才加 --confirm 真下单。",
        )
        await exec_client._disconnect()
        await bm.close()
        return 0

    rc = 1
    try:
        print("\n▶ 真下单(_submit_order,市价 BACK,预期立即成交)…")
        await exec_client._submit_order(SimpleNamespace(order=order))
        await asyncio.sleep(args.settle_wait)
        voi = captured["voi"]
        print(f"  venue_order_id = {voi}")

        bet = exec_client._current_bets.get(str(voi)) if voi else None
        print(f"  CURRENT_BETS 收到 {len(bets_frames)} 帧;该单快照={'有' if bet else '无'}")
        if bet:
            prog = bet_order_progress(bet)
            print(
                "  matched 帧填充值:"
                f" sizeMatched={bet.get('sizeMatched')} averagePrice={bet.get('averagePrice')}"
                f" sizeRemaining={bet.get('sizeRemaining')} price={bet.get('price')}",
            )
            print(f"  reconcile 派生:status={prog.get('status') if prog else '?'} "
                  f"filled_qty={prog.get('filled_qty') if prog else '?'} avg_px={prog.get('avg_px') if prog else '?'}")
        print(f"  generate_order_filled 触发 {len(captured['fills'])} 次:")
        for f in captured["fills"]:
            print(f"    last_qty={f['last_qty']} last_px={f['last_px']} liquidity={f['liquidity']} side={f['side']}")

        matched = bool(bet) and float(bet.get("sizeMatched", 0) or 0) > 0
        filled_event = len(captured["fills"]) > 0
        avg_priced = bool(bet) and float(bet.get("averagePrice", 0) or 0) > 0

        print("\n══ Gap C Tier 2 观测结论 ══")
        print(f"  [{'✓' if matched else '✗'}] matched 帧 sizeMatched>0")
        print(f"  [{'✓' if avg_priced else '✗'}] matched 帧 averagePrice>0(fill 均价)")
        print(f"  [{'✓' if filled_event else '✗'}] generate_order_filled 触发(last_px/liquidity=MAKER 假设)")
        rc = 0 if (matched and filled_event) else 1
    finally:
        # 成交不可撤;不自动平仓(用户选手动处理)。显著告警。
        voi = captured["voi"]
        rem = None
        if voi:
            b = exec_client._current_bets.get(str(voi))
            rem = float(b.get("sizeRemaining", 0) or 0) if b else None
        print("\n" + "=" * 60)
        print("⚠️  你现在可能持有一笔真实 BACK 仓(成交不可撤)。")
        print(f"    venue_order_id(offerId)= {voi}")
        if rem:
            print(f"    ⚠️ 还有未成交余量 sizeRemaining={rem} —— 可用 "
                  f"`gapc_place_cancel_probe.py --cleanup-only` 撤掉这部分 working 单。")
        print("    请到 OE 页面自行决定平仓 / 持仓。")
        print("=" * 60)
        await exec_client._disconnect()
        await bm.close()
    return rc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OE Gap C FILL probe (Tier 2, marketable BACK — WILL FILL, real position)")
    p.add_argument("--config", required=True, help="path to arb_config.json")
    p.add_argument("--confirm", action="store_true", help="真下单(会成交!不加则 dry-run 只打印将下的单)")
    p.add_argument("--headed", action="store_true", help="强制可见浏览器")
    p.add_argument("--size", type=float, default=_MIN_OE_STAKE, help="stake(OE 最小允许,默认 7;= 最大损失)")
    p.add_argument("--settle-wait", type=float, default=8.0, help="下单后等 matched WS 帧的秒数")
    args = p.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
