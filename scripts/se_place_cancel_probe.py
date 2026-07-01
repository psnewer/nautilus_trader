"""SharpExch 真单验证探针(Tier 1:place + cancel,预期不成交)。

验证 SE ExecutionClient 的 live place/cancel 边界:
  真 Playwright 登录 → 真实 `sport/details` 发现 instrument →
  `_submit_order` 调 `/customer/api/placeBets` → `CURRENT_BETS` 反映 working 单 →
  `_cancel_order` 调 `/customer/api/cancelBets` → 确认 remaining=0 或单消失。

安全边界:
  - 默认 dry-run:连接 + 发现 + 构单 + 打印将要下的单,但不调用 placeBets。
  - 只有显式 `--confirm` 才真下单。
  - finally 按 CURRENT_BETS 逐单兜底 cancel,避免留下 SE 活单。

用法:
  python3 -m scripts.se_place_cancel_probe --config arb_config.json --headed
  python3 -m scripts.se_place_cancel_probe --config arb_config.json --headed --confirm --size 7
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from nautilus_trader.adapters.sharpexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.sharpexch.discovery_client import SharpExchDiscoveryClient
from nautilus_trader.adapters.sharpexch.discovery_client import SharpExchMarketEvent
from nautilus_trader.adapters.sharpexch.discovery_client import SharpExchRunner
from nautilus_trader.adapters.sharpexch.discovery_client import SharpExchSportDetailsRequest
from nautilus_trader.adapters.sharpexch.discovery_client import events_from_sport_details
from nautilus_trader.adapters.sharpexch.discovery_client import sport_details_request
from nautilus_trader.adapters.sharpexch.execution import SHARPEXCH
from nautilus_trader.adapters.sharpexch.execution import SharpExchExecutionClient
from nautilus_trader.adapters.sharpexch.execution import bet_order_progress
from nautilus_trader.adapters.sharpexch.message_parser import SharpExchMessageParser
from nautilus_trader.adapters.sharpexch.providers import SharpExchInstrumentProvider
from nautilus_trader.adapters.sharpexch.web import se_customer_context
from nautilus_trader.adapters.sharpexch.web import se_dismiss_post_login_popup
from nautilus_trader.adapters.sharpexch.web import se_fetch_json
from nautilus_trader.adapters.sharpexch.web import se_is_customer_url
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.config import load_arb_config
from src.arbitrage.config.dispatcher import to_se_discovery_config
from src.arbitrage.config.dispatcher import to_sharpexch_exec_client_config


_NON_MARKETABLE_BACK_ODDS = 100.0
_MIN_SE_STAKE_USD = 12.0


def _inst_display(inst) -> str:
    return (
        f"  instrument={inst.id}\n"
        f"  competition={getattr(inst, 'competition_name', '')}\n"
        f"  event={getattr(inst, 'event_name', '')}\n"
        f"  selection={getattr(inst, 'selection_name', '')} role={inst.info.get('selection_role')}\n"
        f"  market_id={getattr(inst, 'market_id', '')} selection_id={getattr(inst, 'selection_id', '')}"
    )


def _instrument_from_args(cfg, args):
    if not args.market_id or not args.selection_id:
        return None
    role = args.selection_role
    event = SharpExchMarketEvent(
        sport="Tennis",
        competition=args.competition_name,
        home_team=args.home_team,
        away_team=args.away_team,
        sport_id=args.sport_id,
        competition_id=args.competition_id,
        market_id=args.market_id,
        start_ts=0,
        runners=(
            SharpExchRunner(
                selection_id=str(args.selection_id),
                runner_name=role,
                role=role,
            ),
        ),
    )
    provider = SharpExchInstrumentProvider(
        discovery=SharpExchDiscoveryClient(),
        sport_aliases=dict(cfg.matching.sport_aliases),
        competition_aliases=dict(cfg.matching.competition_aliases),
        fx=cfg.arbitrage.fx,
    )
    return next(iter(provider._build_legs(event)), None)


def _active_bets(current_bets: dict) -> dict:
    return {
        offer_id: bet
        for offer_id, bet in current_bets.items()
        if float(bet.get("sizeRemaining", 0) or 0) > 0
    }


async def _discover_instruments(
    exec_client: SharpExchExecutionClient,
    cfg,
    *,
    fetch_timeout_ms: int,
    challenge_wait_secs: float,
):
    """在已登录 execution page 的 customer context 中走 SE 发现路径。"""

    discovery_cfg = to_se_discovery_config(cfg)
    sport_configs = list(getattr(discovery_cfg, "sports", []) or []) if discovery_cfg is not None else []
    target_competitions = [
        comp
        for sport in sport_configs
        for comp in getattr(sport, "competitions", []) or []
    ]

    async def _fetch_once(request: SharpExchSportDetailsRequest) -> dict:
        context = await _ensure_customer_fetch_context(exec_client)
        context_url = getattr(context, "url", "")
        print(
            "  sport/details request: "
            f"page={request.params.get('page')} size={request.params.get('size')} "
            f"body_id={request.body.get('id')} context={context_url}",
            flush=True,
        )
        payload = await se_fetch_json(
            context,
            request.url,
            params=request.params,
            body=request.body,
            timeout_ms=fetch_timeout_ms,
        )
        print(
            "  sport/details response: "
            f"status={payload.get('status')} ok={payload.get('ok')} "
            f"text={payload.get('text')!r}",
            flush=True,
        )
        if not payload.get("ok") or not isinstance(payload.get("json"), dict):
            raise RuntimeError(
                f"SE sport/details failed: status={payload.get('status')} text={payload.get('text')!r}",
            )
        return payload["json"]

    async def _fetch(request: SharpExchSportDetailsRequest) -> dict:
        try:
            return await _fetch_once(request)
        except RuntimeError as exc:
            if "status=403" not in str(exc) or "Just a moment" not in str(exc):
                raise
            print(
                "  Cloudflare challenge detected;等待浏览器完成验证后重试 sport/details..."
                f" wait={challenge_wait_secs}s",
                flush=True,
            )
            try:
                await exec_client._page.bring_to_front()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(max(0.0, challenge_wait_secs))
            return await _fetch_once(request)

    first_config = sport_configs[0] if sport_configs else None
    request = sport_details_request(exec_client._config.base_url, first_config, page=0, size=60)
    payload = await _fetch(request)
    events = events_from_sport_details(
        payload,
        target_competitions=target_competitions or request.target_competitions,
    )
    print(f"  page0 解析 {len(events)} 个 SE events")
    provider = SharpExchInstrumentProvider(
        discovery=SharpExchDiscoveryClient(),
        sport_aliases=dict(cfg.matching.sport_aliases),
        competition_aliases=dict(cfg.matching.competition_aliases),
        sport_configs=sport_configs,
        fx=cfg.arbitrage.fx,
    )
    for event in events:
        for instrument in provider._build_legs(event):
            provider.add(instrument)
    instruments = provider.list_all()
    print(f"  发现 {len(instruments)} 条 SE instrument")
    return instruments


async def _ensure_customer_fetch_context(exec_client: SharpExchExecutionClient):
    context = se_customer_context(exec_client._page)
    if se_is_customer_url(getattr(context, "url", "")):
        return context
    customer_url = f"{exec_client._config.base_url.rstrip('/')}/customer"
    print(f"  customer context missing; navigate direct: {customer_url}", flush=True)
    await exec_client._page.goto(
        customer_url,
        wait_until="domcontentloaded",
        timeout=exec_client._config.page_timeout,
    )
    return se_customer_context(exec_client._page)


async def _cancel_active_bets(exec_client: SharpExchExecutionClient, *, settle_wait: float) -> dict:
    active = _active_bets(exec_client._current_bets)
    if not active:
        return {}
    print(f"  发现 SE 活单 {len(active)} 条,执行兜底 cancel...")
    for offer_id, bet in list(active.items()):
        result = await exec_client._executor.cancel_order(
            str(bet.get("marketId", "")),
            str(offer_id),
            exec_client._page,
        )
        print(
            f"    offerId={offer_id} market={bet.get('marketId')} "
            f"remaining={bet.get('sizeRemaining')} cancel_success={result.get('success')} "
            f"message={result.get('message')}",
        )
    await asyncio.sleep(settle_wait)
    return _active_bets(exec_client._current_bets)


async def _open_competition_and_wait_market(exec_client: SharpExchExecutionClient, inst, *, timeout: float) -> dict | None:
    parser = SharpExchMessageParser()
    seen: dict[str, dict] = {}
    market_id = str(getattr(inst, "market_id", ""))

    def _on_price(message):
        parsed = parser.parse_price_message(message)
        if parsed and str(parsed.get("market_id")) == market_id:
            seen["target"] = parsed

    exec_client._ws_handler.on_price_update(_on_price)
    url = (
        f"{exec_client._config.base_url.rstrip('/')}/customer/sport/"
        f"{getattr(inst, 'event_type_id', '')}/competition/{getattr(inst, 'competition_id', '')}"
    )
    print(f"▶ open competition page before place: {url}")
    await exec_client._page.goto(url, wait_until="domcontentloaded", timeout=exec_client._config.page_timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "target" in seen:
            return seen["target"]
        await asyncio.sleep(0.2)
    return None


async def _open_competition_and_wait_prices(
    exec_client: SharpExchExecutionClient,
    inst,
    *,
    timeout: float,
) -> dict[str, dict]:
    parser = SharpExchMessageParser()
    seen: dict[str, dict] = {}

    def _on_price(message):
        parsed = parser.parse_price_message(message)
        if parsed and parsed.get("market_id"):
            seen[str(parsed["market_id"])] = parsed

    exec_client._ws_handler.on_price_update(_on_price)
    url = (
        f"{exec_client._config.base_url.rstrip('/')}/customer/sport/"
        f"{getattr(inst, 'event_type_id', '')}/competition/{getattr(inst, 'competition_id', '')}"
    )
    print(f"▶ open competition page before place: {url}")
    await exec_client._page.goto(url, wait_until="domcontentloaded", timeout=exec_client._config.page_timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if seen:
            return seen
        await asyncio.sleep(0.2)
    return seen


async def run(args) -> int:
    if args.size < _MIN_SE_STAKE_USD:
        print(f"✗ --size={args.size} 低于 SE 最小 stake {_MIN_SE_STAKE_USD};请使用 --size {_MIN_SE_STAKE_USD} 或更高")
        return 2

    _load_dotenv()
    cfg = load_arb_config(args.config)
    exec_cfg = to_sharpexch_exec_client_config(cfg)
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
        msgbus=MessageBus(trader_id=TraderId("PROBE-SE-000"), clock=clock),
        cache=cache,
        clock=clock,
        instrument_provider=InstrumentProvider(),
        config=exec_cfg,
        venue_liveness=VenueExecutionLiveness([SHARPEXCH]),
        fx=cfg.arbitrage.fx,
    )

    captured = {"voi": None, "rejected": None}
    place_results: list[dict] = []
    bets_frames: list[float] = []
    orig_accepted = exec_client.generate_order_accepted
    orig_rejected = exec_client.generate_order_rejected
    orig_bets = exec_client._on_current_bets

    def _accepted_spy(*, strategy_id, instrument_id, client_order_id, venue_order_id, ts_event, **kwargs):
        captured["voi"] = venue_order_id
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

    def _bets_spy(bets):
        bets_frames.append(time.time())
        return orig_bets(bets)

    exec_client.generate_order_accepted = _accepted_spy
    exec_client.generate_order_rejected = _rejected_spy
    exec_client._on_current_bets = _bets_spy
    orig_place_via_executor = exec_client._place_via_executor

    async def _place_spy(order):
        result = await orig_place_via_executor(order)
        if isinstance(result, dict):
            place_results.append(result)
        return result

    exec_client._place_via_executor = _place_spy

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
        if args.cleanup_only:
            print("\n▶ cleanup-only:等待 CURRENT_BETS 后撤活单…")
            await asyncio.sleep(args.settle_wait)
            active = await _cancel_active_bets(exec_client, settle_wait=args.settle_wait)
            print(f"  cleanup 后活单数={len(active)}")
            return 0 if not active else 1

        manual_inst = _instrument_from_args(cfg, args)
        if manual_inst is not None:
            instruments = [manual_inst]
            inst = manual_inst
            print("\n▶ SE 使用命令行指定 instrument(跳过 sport/details discovery)…")
        else:
            print("\n▶ SE 发现(取一条真实 instrument)…")
            await se_dismiss_post_login_popup(exec_client._page, timeout_ms=1500)
            try:
                instruments = await asyncio.wait_for(
                    _discover_instruments(
                        exec_client,
                        cfg,
                        fetch_timeout_ms=args.discovery_fetch_timeout_ms,
                        challenge_wait_secs=args.challenge_wait,
                    ),
                    timeout=args.discovery_timeout,
                )
            except TimeoutError:
                print(f"✗ SE discovery 超时 {args.discovery_timeout}s;尚未进入下单阶段")
                return 1
            if not instruments:
                print("✗ 未发现任何 SE instrument;终止")
                return 1
            inst = instruments[0]
        cache.add_instrument(inst)
        print("  选用 instrument:\n" + _inst_display(inst))
        price_snapshots = await _open_competition_and_wait_prices(exec_client, inst, timeout=args.price_wait)
        if price_snapshots:
            alive_market_ids = set(price_snapshots)
            alive_inst = next(
                (
                    candidate
                    for candidate in instruments
                    if str(getattr(candidate, "market_id", "")) in alive_market_ids
                ),
                None,
            )
            if alive_inst is not None and alive_inst.id != inst.id:
                inst = alive_inst
                cache.add_instrument(inst)
                print("  改用 prices WS 中出现的 instrument:\n" + _inst_display(inst))
            price_snapshot = price_snapshots.get(str(getattr(inst, "market_id", "")))
        else:
            price_snapshot = None
        if price_snapshot is None:
            print(f"  ⚠️ {args.price_wait}s 内没有收到可匹配的价格帧;不确认该 market 当前可下单")
        else:
            runners = price_snapshot.get("runners") or []
            status = price_snapshot.get("status")
            in_play = price_snapshot.get("in_play")
            print(
                f"  price snapshot: status={status} in_play={in_play} "
                f"runners={len(runners)} target_market={price_snapshot.get('market_id')}",
            )

        factory = OrderFactory(
            trader_id=TraderId("PROBE-SE-000"),
            strategy_id=StrategyId("PROBE-S"),
            clock=clock,
        )
        side = OrderSide.BUY if args.side.upper() == "BUY" else OrderSide.SELL
        order = factory.limit(
            inst.id,
            side,
            inst.make_qty(args.size),
            inst.make_price(args.odds),
        )
        cache.add_order(order)
        liability = round((args.odds - 1.0) * args.size, 2) if side == OrderSide.SELL else args.size
        print(
            f"\n── 将要下的 SE 单 ──\n{_inst_display(inst)}\n"
            f"  side={side.name}({'BACK' if side == OrderSide.BUY else 'LAY'})\n"
            f"  odds={args.odds}(预期不成交保护价)\n"
            f"  size={args.size} USD\n"
            f"  est_liability={liability} USD\n"
            f"  client_order_id={order.client_order_id}",
        )

        if not args.confirm:
            print("\n⏸ dry-run(未加 --confirm):不调用 placeBets。")
            return 0

        rc = 1
        print("\n▶ 真下单(_submit_order → placeBets)…")
        await se_dismiss_post_login_popup(exec_client._page, timeout_ms=1500)
        await exec_client._submit_order(SimpleNamespace(order=order))
        await asyncio.sleep(args.settle_wait)
        voi = captured["voi"]
        print(f"  venue_order_id={voi} rejected={captured['rejected']}")
        if place_results:
            print("  placeBets response=" + _compact_json(_sanitize(place_results[-1])))

        bet = exec_client._current_bets.get(str(voi)) if voi else None
        print(f"  CURRENT_BETS 收到 {len(bets_frames)} 帧;该单快照={'有' if bet else '无'}")
        if bet:
            prog = bet_order_progress(bet)
            print(
                f"  派生:status={prog.get('status') if prog else '?'} "
                f"side={prog.get('side') if prog else '?'} "
                f"remaining={bet.get('sizeRemaining')} matched={bet.get('sizeMatched')}",
            )
        reports = await exec_client.generate_order_status_reports(SimpleNamespace())
        print(f"  generate_order_status_reports 派生 {len(reports)} 条 report")

        submit_ok = voi is not None
        bet_working = bool(bet) and float(bet.get("sizeRemaining", 0) or 0) > 0

        print("\n▶ 真撤单(_cancel_order → cancelBets)…")
        await exec_client._cancel_order(
            SimpleNamespace(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=voi,
            ),
        )
        await asyncio.sleep(args.settle_wait)
        cancel_bet = exec_client._current_bets.get(str(voi)) if voi else None
        cancel_ok = (cancel_bet is None) or float(cancel_bet.get("sizeRemaining", 0) or 0) == 0

        print("\n══ SE Tier 1 观测结论 ══")
        print(f"  [{'✓' if submit_ok else '✗'}] _submit_order 真下单 → venue_order_id")
        print(f"  [{'✓' if bet_working else '✗'}] CURRENT_BETS 反映 working 单(sizeRemaining>0)")
        print(f"  [{'✓' if cancel_ok else '✗'}] _cancel_order 撤掉(CURRENT_BETS 该单消失/remaining=0)")
        rc = 0 if (submit_ok and bet_working and cancel_ok) else 1
        return rc
    finally:
        try:
            print("\n▶ 兜底撤 SE 活单…")
            active = await _cancel_active_bets(exec_client, settle_wait=args.settle_wait)
            print(f"  兜底后活单数={len(active)}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ 兜底撤单异常:{exc!r} —— 请手动到 SE 确认无残留挂单!")
        await exec_client._disconnect()
        await bm.close()


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(_PROJECT_ROOT / ".env")
    except ImportError:
        return


def _compact_json(value) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) > 1600:
        return text[:1600] + "...<truncated>"
    return text


def _sanitize(value):
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
    if isinstance(value, tuple):
        return [_sanitize(item) for item in value]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SE place-and-cancel probe (Tier 1, non-marketable)")
    parser.add_argument("--config", required=True, help="path to arb_config.json")
    parser.add_argument("--confirm", action="store_true", help="真下单(不加则 dry-run 只打印将下的单)")
    parser.add_argument("--cleanup-only", action="store_true", help="零下单:连接后按 CURRENT_BETS 撤活单")
    parser.add_argument("--headed", action="store_true", help="强制可见浏览器")
    parser.add_argument("--size", type=float, default=_MIN_SE_STAKE_USD, help="stake USD(默认 12)")
    parser.add_argument("--side", choices=["BUY", "SELL"], default="BUY", help="默认 BUY/BACK")
    parser.add_argument("--odds", type=float, default=_NON_MARKETABLE_BACK_ODDS, help="默认 100(BACK 保护价)")
    parser.add_argument("--settle-wait", type=float, default=8.0, help="下单/撤单后等 WS 帧的秒数")
    parser.add_argument("--price-wait", type=float, default=12.0, help="下单前等待目标 market price frame 的秒数")
    parser.add_argument("--discovery-timeout", type=float, default=45.0, help="sport/details 发现总超时秒数")
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
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
