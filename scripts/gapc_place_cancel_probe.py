"""Gap C 真单验证探针(Tier 1:place + cancel,**不成交**)。

验证 OE ExecClient 的 live 未知(refactor.md Gap C / [[gap_c_oe_exec_live_validated]]):
  真 Playwright `_submit_order` 下单 → `venue_order_id` → `CURRENT_BETS` 反映 working 单 →
  真 `generate_order_status_reports` 从 CURRENT_BETS 派生该单 → 真 `_cancel_order` 撤掉。

**不成交设计**:LAY(SELL)@ 赔率 **1.01** + OE 最小 stake 7。
BACK 方向 odds 越大越好,所以 BACK@1.01 是最差价格,不能作为不成交保护;LAY@1.01 的
潜在 liability 很小,且通常不会被已有 BACK 盘穿透。走**完整 `_submit_order`/`_cancel_order`**(含 session + tracking;
tracking 超时只结束 session 不自动撤/不补偿,见 session.py:11,故安全)。

**真账户、真登录、真下单**(Tier 1 不成交)。两道安全闸:
  1. **默认 dry-run**:连接 + 发现 + 构单 + 打印"将要下的单",但**不 place**;加 `--confirm` 才真下单。
  2. **撤单兜底**:正常走 `_cancel_order`;无论成败,finally 再 `_cancel_all_orders` 清掉任何残留
     (防 [[bug_compensating_cancel_missing]] 留活单)。

用法:
  # 先 dry-run 看将要下的单(不下单):
  python3 -m scripts.gapc_place_cancel_probe --config arb_config.json --headed
  # 确认无误后真下单 + 撤单:
  python3 -m scripts.gapc_place_cancel_probe --config arb_config.json --headed --confirm --size 7
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
from src.arbitrage.config.dispatcher import to_oe_scraper_config
from src.arbitrage.config.dispatcher import to_orbitexch_exec_client_config

_NON_MARKETABLE_ODDS = 1.01  # LAY@1.01:低 liability,通常不穿透已有 BACK 盘
_MIN_OE_STAKE = 7.0


def _inst_display(inst) -> str:
    """格式化真实 instrument 身份,供 dry-run 人工确认真单目标。"""
    return (
        f"  instrument={inst.id}\n"
        f"  competition={getattr(inst, 'competition_name', '')}\n"
        f"  event={getattr(inst, 'event_name', '')}\n"
        f"  selection={getattr(inst, 'selection_name', '')} role={inst.info.get('selection_role')}\n"
        f"  market_id={getattr(inst, 'market_id', '')} selection_id={getattr(inst, 'selection_id', '')}"
    )


def _active_bets(current_bets: dict) -> dict:
    """CURRENT_BETS 中 sizeRemaining>0 的活单。remaining=0 的已撤记录不算残单。"""
    return {
        offer_id: bet for offer_id, bet in current_bets.items()
        if float(bet.get("sizeRemaining", 0) or 0) > 0
    }


async def _discover_one_instrument(cfg):
    """走生产同款 OE 发现路径,返第一条有 selection_id 的真实 BettingInstrument。"""
    scraper_cfg = to_oe_scraper_config(cfg)
    if scraper_cfg is None:
        print("✗ oe_scraper_config 为空(discovery 未配)— 无法发现 instrument")
        return None
    from nautilus_trader.adapters.orbitexch.discovery_scraper import OrbitExchScraper
    from nautilus_trader.adapters.orbitexch.providers import OrbitExchInstrumentProvider

    scraper = OrbitExchScraper(config=scraper_cfg)
    provider = OrbitExchInstrumentProvider(
        scraper=scraper,
        sport_aliases=dict(cfg.matching.sport_aliases),
        competition_aliases=dict(cfg.matching.competition_aliases),
    )
    await provider.load_all_async()
    insts = provider.list_all()
    print(f"  发现 {len(insts)} 条 instrument")
    return insts[0] if insts else None


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

    # ── 1. 发现一条真实 instrument(生产同款路径,独立 scraper 浏览器)──
    print("▶ OE 发现(取一条真实 instrument)…")
    inst = await _discover_one_instrument(cfg)
    if inst is None:
        print("✗ 未发现任何 instrument;终止(检查 discovery 配置 / 该 competition 是否有进行中赛事)")
        return 1
    print("  选用 instrument:\n" + _inst_display(inst))

    # ── 2. 连接 ExecClient(真登录)──
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
        msgbus=MessageBus(trader_id=TraderId("PROBE-GAPC-000"), clock=clock),
        cache=cache, clock=clock,
        instrument_provider=InstrumentProvider(),
        config=exec_cfg, leg_settled=LegSettledRegistry(),
    )

    # 观测埋点:捕获 generate_order_accepted 的 venue_order_id + CURRENT_BETS 帧
    captured = {"voi": None}
    bets_frames: list[float] = []
    orig_accepted = exec_client.generate_order_accepted
    orig_bets = exec_client._on_current_bets

    def _accepted_spy(*, strategy_id, instrument_id, client_order_id, venue_order_id, ts_event, **k):
        captured["voi"] = venue_order_id
        return orig_accepted(strategy_id=strategy_id, instrument_id=instrument_id,
                             client_order_id=client_order_id, venue_order_id=venue_order_id,
                             ts_event=ts_event, **k)

    def _bets_spy(bets):
        bets_frames.append(time.time())
        return orig_bets(bets)

    exec_client.generate_order_accepted = _accepted_spy
    exec_client._on_current_bets = _bets_spy

    print(f"▶ 连接(headless={headless},真登录)…")
    try:
        await exec_client._connect()
    except Exception as e:  # noqa: BLE001
        print(f"✗ _connect 失败:{e!r}")
        await bm.close()
        return 1

    if args.cleanup_only:
        print("\n▶ cleanup-only:先等待 CURRENT_BETS 帧…")
        await asyncio.sleep(args.settle_wait)
        print(f"  cancel 前 CURRENT_BETS 活单数={len(exec_client._current_bets)}")
        for offer_id, bet in exec_client._current_bets.items():
            print(
                f"    offerId={offer_id} side={bet.get('side')} price={bet.get('price')} "
                f"remaining={bet.get('sizeRemaining')} matched={bet.get('sizeMatched')} "
                f"market={bet.get('marketId')} selection={bet.get('selectionId')}"
            )
        if exec_client._current_bets and exec_client._executor is not None:
            from src.arbitrage.common.order_models import Order as _Order
            from src.arbitrage.common.order_models import Venue as _Venue

            print("\n▶ cleanup-only:逐单 cancel_order(按 CURRENT_BETS marketId + offerId)…")
            for offer_id, bet in list(exec_client._current_bets.items()):
                legacy = _Order(
                    venue=_Venue.ORBITEXCH,
                    venue_order_id=str(offer_id),
                    market_id=str(bet.get("marketId", "")),
                    selection_id=str(bet.get("selectionId", "")),
                )
                result = await exec_client._executor.cancel_order(legacy, exec_client._page)
                print(
                    f"    offerId={offer_id} cancel_order success={getattr(result, 'success', None)} "
                    f"message={getattr(result, 'message', '')}"
                )
            await asyncio.sleep(args.settle_wait)
        print("\n▶ cleanup-only:_cancel_all_orders…")
        await exec_client._cancel_all_orders(SimpleNamespace(instrument_id=inst.id))
        await asyncio.sleep(args.settle_wait)
        active = _active_bets(exec_client._current_bets)
        print(f"  cancel 后 CURRENT_BETS 记录数={len(exec_client._current_bets)},活单数={len(active)}")
        for offer_id, bet in exec_client._current_bets.items():
            print(
                f"    offerId={offer_id} side={bet.get('side')} price={bet.get('price')} "
                f"remaining={bet.get('sizeRemaining')} matched={bet.get('sizeMatched')} "
                f"market={bet.get('marketId')} selection={bet.get('selectionId')}"
            )
        await exec_client._disconnect()
        await bm.close()
        return 0 if not active else 1

    # ── 3. 构单(LAY@1.01,OE 最小注)──
    factory = OrderFactory(trader_id=TraderId("PROBE-GAPC-000"), strategy_id=StrategyId("PROBE-S"), clock=clock)
    order = factory.limit(
        inst.id, OrderSide.SELL, inst.make_qty(args.size), inst.make_price(_NON_MARKETABLE_ODDS),
    )
    cache.add_order(order)
    liability = round((_NON_MARKETABLE_ODDS - 1.0) * args.size, 2)
    print(
        f"\n── 将要下的单 ──\n{_inst_display(inst)}\n  side=SELL(LAY)\n"
        f"  odds={_NON_MARKETABLE_ODDS}(低 liability,预期不成交)\n  size={args.size}\n"
        f"  est_liability={liability} GBP\n"
        f"  client_order_id={order.client_order_id}",
    )

    if not args.confirm:
        print(
            "\n⏸  dry-run(未加 --confirm):不下单。"
            "只有在确认 side=SELL(LAY)、odds=1.01、size>=7 且账户可承受该风险后,才加 --confirm 重跑真下单。"
        )
        await exec_client._disconnect()
        await bm.close()
        return 0

    # ── 4. 真下单 → 观测 → 撤单(撤单兜底在 finally）──
    rc = 1
    try:
        print("\n▶ 真下单(_submit_order)…")
        await exec_client._submit_order(SimpleNamespace(order=order))
        await asyncio.sleep(args.settle_wait)
        voi = captured["voi"]
        print(f"  venue_order_id = {voi}")

        # CURRENT_BETS working 态 + reconcile 派生
        bet = exec_client._current_bets.get(str(voi)) if voi else None
        print(f"  CURRENT_BETS 收到 {len(bets_frames)} 帧;该单快照={'有' if bet else '无'}")
        if bet:
            prog = bet_order_progress(bet)
            print(f"  reconcile 派生:status={prog.get('status') if prog else '?'} "
                  f"side={prog.get('side') if prog else '?'} "
                  f"working={bet.get('sizeRemaining')} matched={bet.get('sizeMatched')}")
        reports = await exec_client.generate_order_status_reports(SimpleNamespace())
        print(f"  generate_order_status_reports 派生 {len(reports)} 条 report")

        submit_ok = voi is not None
        bet_working = bool(bet) and float(bet.get("sizeRemaining", 0) or 0) > 0

        print("\n▶ 撤单(_cancel_order)…")
        await exec_client._cancel_order(SimpleNamespace(
            strategy_id=order.strategy_id, instrument_id=order.instrument_id,
            client_order_id=order.client_order_id, venue_order_id=voi,
        ))
        await asyncio.sleep(args.settle_wait)
        cancel_bet = exec_client._current_bets.get(str(voi)) if voi else None
        cancel_ok = (cancel_bet is None) or float(cancel_bet.get("sizeRemaining", 0) or 0) == 0

        print("\n══ Gap C Tier 1 观测结论 ══")
        print(f"  [{'✓' if submit_ok else '✗'}] _submit_order 真下单 → venue_order_id")
        print(f"  [{'✓' if bet_working else '✗'}] CURRENT_BETS 反映 working 单(sizeRemaining>0)")
        print(f"  [{'✓' if cancel_ok else '✗'}] _cancel_order 撤掉(CURRENT_BETS 该单消失/remaining=0)")
        rc = 0 if (submit_ok and bet_working and cancel_ok) else 1
    finally:
        # 撤单兜底:无论上面如何,确保不留活单(防 bug_compensating_cancel_missing)
        try:
            print("\n▶ 兜底 _cancel_all_orders(确保无残留活单)…")
            await exec_client._cancel_all_orders(SimpleNamespace(instrument_id=inst.id))
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 兜底撤单异常:{e!r} —— 请手动到 OE 确认无残留挂单!")
        await exec_client._disconnect()
        await bm.close()
    return rc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OE Gap C place-and-cancel probe (Tier 1, non-marketable)")
    p.add_argument("--config", required=True, help="path to arb_config.json")
    p.add_argument("--confirm", action="store_true", help="真下单(不加则 dry-run 只打印将下的单)")
    p.add_argument("--cleanup-only", action="store_true", help="零下单:连接后 cancel-all 并打印 CURRENT_BETS")
    p.add_argument("--headed", action="store_true", help="强制可见浏览器")
    p.add_argument("--size", type=float, default=_MIN_OE_STAKE, help="stake(OE 最小允许,默认 7)")
    p.add_argument("--settle-wait", type=float, default=8.0, help="下单/撤单后等 WS 帧的秒数")
    args = p.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
