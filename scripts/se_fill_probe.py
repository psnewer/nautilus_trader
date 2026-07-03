"""SharpExch 真成交验证探针(Tier 2:会成交,真金真持仓)。

验证 SE place/cancel 未覆盖的成交路径:
  真成交后 `CURRENT_BETS` 的 matched 字段(`sizeMatched>0`/`averagePrice>0`)+
  `current_bets_to_fills` 计算 delta + `generate_order_filled` 产出是否正确。

⚠️⚠️ 本探针会真成交、真金真持仓 ⚠️⚠️
  - 默认构造 BUY/BACK @ 1.01:按 exchange 语义会吃可用对手盘,最大损失约等于 stake。
  - 成交不可撤;脚本只会在有未成交余量时撤掉剩余 working 部分,不会平掉已成交仓位。
  - 默认 dry-run:只登录、发现、打印将下的单;只有显式 `--confirm` 才调用 placeBets。

用法:
  python3 -m scripts.se_fill_probe --config arb_config.json --headed
  python3 -m scripts.se_fill_probe --config arb_config.json --headed --confirm --size 12
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

from nautilus_trader.adapters.sharpexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.sharpexch.config import SharpExchExecClientConfig
from nautilus_trader.adapters.sharpexch.execution import SHARPEXCH
from nautilus_trader.adapters.sharpexch.execution import SharpExchExecutionClient
from nautilus_trader.adapters.sharpexch.execution import bet_order_progress
from nautilus_trader.adapters.sharpexch.web import se_dismiss_post_login_popup
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from scripts.se_place_cancel_probe import _MIN_SE_STAKE_USD
from scripts.se_place_cancel_probe import _cancel_active_bets
from scripts.se_place_cancel_probe import _discover_instruments
from scripts.se_place_cancel_probe import _instrument_from_args
from scripts.se_place_cancel_probe import _inst_display
from scripts.se_place_cancel_probe import _load_dotenv
from scripts.se_place_cancel_probe import _open_competition_and_wait_prices
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.config import load_arb_config
from src.arbitrage.config.dispatcher import to_sharpexch_exec_client_config


_MARKETABLE_BACK_ODDS = 1.01


def _exec_config_with_user_data_dir(config: SharpExchExecClientConfig, user_data_dir: str) -> SharpExchExecClientConfig:
    """返回仅覆盖 browser profile 的 SE exec config。"""

    if not user_data_dir:
        return config
    return SharpExchExecClientConfig(
        username=config.username,
        password=config.password,
        base_url=config.base_url,
        login_url=config.login_url,
        headless=config.headless,
        browser_type=config.browser_type,
        user_data_dir=user_data_dir,
        page_timeout=config.page_timeout,
        max_bet_amount=config.max_bet_amount,
        confirm_bet=config.confirm_bet,
    )


def _record_accepted_probe_cache(cache, client_order_id, venue_order_id) -> None:
    """direct-client probe 中模拟 ExecEngine apply accepted event 后的 cache 映射。"""

    cache.add_venue_order_id(client_order_id, venue_order_id)


async def _selected_instrument(exec_client, cfg, args, cache):
    manual_inst = _instrument_from_args(cfg, args)
    if manual_inst is not None:
        instruments = [manual_inst]
        inst = manual_inst
        print("\n▶ SE 使用命令行指定 instrument(跳过 sport/details discovery)…")
    else:
        print("\n▶ SE 发现(取一条真实 instrument)…")
        await se_dismiss_post_login_popup(exec_client._page, timeout_ms=1500)
        instruments = await asyncio.wait_for(
            _discover_instruments(
                exec_client,
                cfg,
                fetch_timeout_ms=args.discovery_fetch_timeout_ms,
                challenge_wait_secs=args.challenge_wait,
            ),
            timeout=args.discovery_timeout,
        )
        if not instruments:
            return None, False
        inst = instruments[0]

    cache.add_instrument(inst)
    print("  初选 instrument:\n" + _inst_display(inst))
    price_snapshots = await _open_competition_and_wait_prices(
        exec_client,
        inst,
        timeout=args.price_wait,
        target_market_id=str(getattr(inst, "market_id", "") or ""),
    )
    if not price_snapshots:
        print(f"  ⚠️ {args.price_wait}s 内没有收到价格帧;无法确认该 market 当前可成交")
        return inst, False

    live_market_ids = set(price_snapshots)
    print(f"  price frames markets={sorted(live_market_ids)[:8]}")
    for market_id in sorted(live_market_ids)[:5]:
        runners = price_snapshots.get(market_id, {}).get("runners") or []
        runner_ids = [str(runner.get("selection_id", "")) for runner in runners[:4]]
        print(f"    price market {market_id} runner_ids={runner_ids}")
    live_inst = next(
        (
            candidate
            for candidate in instruments
            if str(getattr(candidate, "market_id", "")) in live_market_ids
        ),
        None,
    )
    if live_inst is not None:
        inst = live_inst
        cache.add_instrument(inst)
    snapshot = price_snapshots.get(str(getattr(inst, "market_id", "")))
    if snapshot:
        print(
            f"  price snapshot: market={snapshot.get('market_id')} "
            f"status={snapshot.get('status')} in_play={snapshot.get('in_play')} "
            f"runners={len(snapshot.get('runners') or [])}",
        )
    else:
        print(f"  ⚠️ 收到 price frame,但没有匹配初选/候选 instruments 的 market_id={getattr(inst, 'market_id', '')}")
    return inst, snapshot is not None


async def run(args) -> int:
    if args.size < _MIN_SE_STAKE_USD:
        print(f"✗ --size={args.size} 低于 SE 最小 stake {_MIN_SE_STAKE_USD};请使用 --size {_MIN_SE_STAKE_USD} 或更高")
        return 2

    _load_dotenv()
    cfg = load_arb_config(args.config)
    exec_cfg = to_sharpexch_exec_client_config(cfg)
    exec_cfg = _exec_config_with_user_data_dir(exec_cfg, args.user_data_dir)
    if not exec_cfg.username or not exec_cfg.password:
        print("✗ 缺 SHARPEXCH_USERNAME / SHARPEXCH_PASSWORD(env 注入后仍为空)")
        return 2

    headless = exec_cfg.headless and not args.headed
    bm = PlaywrightBrowserManager(
        browser_type=exec_cfg.browser_type,
        headless=headless,
        user_data_dir=exec_cfg.user_data_dir,
    )
    clock = LiveClock()
    cache = TestComponentStubs.cache()
    exec_client = SharpExchExecutionClient(
        loop=asyncio.get_running_loop(),
        browser_manager=bm,
        msgbus=MessageBus(trader_id=TraderId("PROBE-SE-FILL-000"), clock=clock),
        cache=cache,
        clock=clock,
        instrument_provider=InstrumentProvider(),
        config=exec_cfg,
        venue_liveness=VenueExecutionLiveness([SHARPEXCH]),
        fx=cfg.arbitrage.fx,
    )

    captured: dict = {"voi": None, "rejected": None, "fills": []}
    bets_frames: list[float] = []
    orig_accepted = exec_client.generate_order_accepted
    orig_rejected = exec_client.generate_order_rejected
    orig_filled = exec_client.generate_order_filled
    orig_bets = exec_client._on_current_bets

    def _accepted_spy(*, strategy_id, instrument_id, client_order_id, venue_order_id, ts_event, **kwargs):
        captured["voi"] = venue_order_id
        _record_accepted_probe_cache(cache, client_order_id, venue_order_id)
        return orig_accepted(
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            ts_event=ts_event,
            **kwargs,
        )

    def _rejected_spy(*, strategy_id, instrument_id, client_order_id, reason, ts_event, **kwargs):
        captured["rejected"] = reason
        return orig_rejected(
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            reason=reason,
            ts_event=ts_event,
            **kwargs,
        )

    def _filled_spy(*, last_qty, last_px, liquidity_side, order_side, **kwargs):
        captured["fills"].append(
            {
                "last_qty": str(last_qty),
                "last_px": str(last_px),
                "liquidity": str(liquidity_side),
                "side": str(order_side),
            },
        )
        return orig_filled(
            last_qty=last_qty,
            last_px=last_px,
            liquidity_side=liquidity_side,
            order_side=order_side,
            **kwargs,
        )

    def _bets_spy(bets):
        bets_frames.append(time.time())
        return orig_bets(bets)

    exec_client.generate_order_accepted = _accepted_spy
    exec_client.generate_order_rejected = _rejected_spy
    exec_client.generate_order_filled = _filled_spy
    exec_client._on_current_bets = _bets_spy

    print(f"▶ SE 连接(headless={headless},真登录)…")
    try:
        await exec_client._connect()
        dismissed = await se_dismiss_post_login_popup(exec_client._page, timeout_ms=1500)
        print(f"  post-login popup dismissed={dismissed}")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ _connect 失败:{exc!r}")
        await bm.close()
        return 1

    try:
        try:
            inst, has_price = await _selected_instrument(exec_client, cfg, args, cache)
        except TimeoutError:
            print(f"✗ SE discovery 超时 {args.discovery_timeout}s;尚未进入下单阶段")
            return 1
        except RuntimeError as exc:
            print(f"✗ SE discovery 失败:{exc}")
            return 1
        if inst is None:
            print("✗ 未发现任何 SE instrument;终止")
            return 1
        if args.confirm and not has_price:
            print("✗ 未确认目标 market price frame;拒绝真下单")
            return 1

        factory = OrderFactory(
            trader_id=TraderId("PROBE-SE-FILL-000"),
            strategy_id=StrategyId("PROBE-S"),
            clock=clock,
        )
        order = factory.limit(
            inst.id,
            OrderSide.BUY,
            inst.make_qty(args.size),
            inst.make_price(args.odds),
        )
        cache.add_order(order)
        print(
            f"\n── 将要下的 SE 单(⚠️ 会成交、真持仓)──\n{_inst_display(inst)}\n"
            f"  side=BUY(BACK)\n"
            f"  odds={args.odds}(市价,吃可用对手盘)\n"
            f"  size={args.size} USD\n"
            f"  最大损失约 = {args.size} USD(该 selection 输)\n"
            f"  client_order_id={order.client_order_id}",
        )

        if not args.confirm:
            print("\n⏸ dry-run(未加 --confirm):不调用 placeBets。")
            print("确认 BUY/BACK、odds=1.01、size、最大损失可承受后,才加 --confirm 真下单。")
            return 0

        print("\n▶ 真下单(_submit_order,市价 BACK,预期立即成交)…")
        await se_dismiss_post_login_popup(exec_client._page, timeout_ms=1500)
        await exec_client._submit_order(SimpleNamespace(order=order))
        await asyncio.sleep(args.settle_wait)

        voi = captured["voi"]
        print(f"  venue_order_id={voi} rejected={captured['rejected']}")
        bet = exec_client._current_bets.get(str(voi)) if voi else None
        print(f"  CURRENT_BETS 收到 {len(bets_frames)} 帧;该单快照={'有' if bet else '无'}")
        if bet:
            prog = bet_order_progress(bet)
            print(
                "  matched 帧填充值:"
                f" sizeMatched={bet.get('sizeMatched')} averagePrice={bet.get('averagePrice')}"
                f" sizeRemaining={bet.get('sizeRemaining')} price={bet.get('price')}",
            )
            print(
                f"  reconcile 派生:status={prog.get('status') if prog else '?'} "
                f"filled_qty={prog.get('filled_qty') if prog else '?'} "
                f"avg_px={prog.get('avg_px') if prog else '?'}",
            )
        print(f"  generate_order_filled 触发 {len(captured['fills'])} 次:")
        for fill in captured["fills"]:
            print(
                f"    last_qty={fill['last_qty']} last_px={fill['last_px']} "
                f"liquidity={fill['liquidity']} side={fill['side']}",
            )

        matched = bool(bet) and float(bet.get("sizeMatched", 0) or 0) > 0
        avg_priced = bool(bet) and float(bet.get("averagePrice", 0) or 0) > 0
        filled_event = len(captured["fills"]) > 0

        print("\n══ SE Tier 2 观测结论 ══")
        print(f"  [{'✓' if matched else '✗'}] matched 帧 sizeMatched>0")
        print(f"  [{'✓' if avg_priced else '✗'}] matched 帧 averagePrice>0(fill 均价)")
        print(f"  [{'✓' if filled_event else '✗'}] generate_order_filled 触发")
        return 0 if (matched and avg_priced and filled_event) else 1
    finally:
        try:
            print("\n▶ 若有未成交余量,兜底撤 SE 活单…")
            active = await _cancel_active_bets(exec_client, settle_wait=args.settle_wait)
            print(f"  兜底后活单数={len(active)}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ 兜底撤单异常:{exc!r} —— 请手动到 SE 确认无残留挂单!")
        if args.confirm and captured["voi"] is not None:
            print("\n" + "=" * 60)
            print("⚠️  若上面 matched>0,你现在持有一笔真实 SE BACK 仓。")
            print(f"    venue_order_id(offerId)= {captured['voi']}")
            print("    脚本不会自动平仓;请到 SE 页面自行决定平仓 / 持仓。")
            print("=" * 60)
        await exec_client._disconnect()
        await bm.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SE fill probe (Tier 2, marketable BACK - WILL FILL)")
    parser.add_argument("--config", required=True, help="path to arb_config.json")
    parser.add_argument("--confirm", action="store_true", help="真下单(会成交;不加则 dry-run)")
    parser.add_argument("--headed", action="store_true", help="强制可见浏览器")
    parser.add_argument("--user-data-dir", default="", help="覆盖 SE browser profile 目录;留空则使用临时无痕式 context")
    parser.add_argument("--size", type=float, default=_MIN_SE_STAKE_USD, help="stake USD(默认 12)")
    parser.add_argument("--odds", type=float, default=_MARKETABLE_BACK_ODDS, help="默认 1.01(BACK 市价)")
    parser.add_argument("--settle-wait", type=float, default=8.0, help="下单后等 matched WS 帧的秒数")
    parser.add_argument("--price-wait", type=float, default=12.0, help="下单前等待目标 market price frame 的秒数")
    parser.add_argument("--discovery-timeout", type=float, default=120.0, help="sport/details 发现总超时秒数")
    parser.add_argument("--discovery-fetch-timeout-ms", type=int, default=15000, help="单次 sport/details fetch 超时毫秒")
    parser.add_argument("--challenge-wait", type=float, default=60.0, help="Cloudflare challenge 出现后等待秒数")
    parser.add_argument("--market-id", default="", help="跳过 discovery 时指定 SE marketId")
    parser.add_argument("--selection-id", default="", help="跳过 discovery 时指定 SE selectionId")
    parser.add_argument("--selection-role", default="home", choices=["home", "away", "draw"])
    parser.add_argument("--sport-id", default="2")
    parser.add_argument("--competition-id", default="12597512")
    parser.add_argument("--competition-name", default="Men's Wimbledon 2026")
    parser.add_argument("--home-team", default="Molcan")
    parser.add_argument("--away-team", default="Altmaier")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
