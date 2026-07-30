"""
StrategyEvaluator —— NT Strategy,唯一运行时策略(Q21):订触发事件 → 查 StrategyRegistry
→ 基于当前 EvalContext 并行 evaluate arb+comp → fire(补偿候选优先)。

设计见 `docs/arbitrage/architectures/strategy/architecture.md §3.5 / §4`。

调用关系:
- `on_data(data)` —— NT msgbus 路由进:OrderBookDeltas / MatchedPair / 自定义信号事件
- `_extract_evaluation_target(data)` —— 拿到 (pair_id, sport, competition);MatchedPair 直读,
  其它 event 经 PairRegistry + instrument.info 反查
- `_evaluate_and_fire(strategy, pair_id)` —— Q19 让路检查 + 挂单基线 + asyncio.gather
  并行 evaluate + 补偿候选优先 fire(actions 走 `await`,异常落进本 task)
- `_on_eval_done(pair_id, task)` —— 评估 task 的唯一 pair 闸出口,**无条件释放**(#261)

**evaluate 无副作用 / fire 在顶层**:`evaluate_tree` 返 EvalResult,本类决定 fire arb 还是 comp。
"""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace
from functools import partial

from nautilus_trader.adapters.polymarket.sports import SPORTS_CLIENT
from nautilus_trader.adapters.polymarket.sports import SportsGameStateStore
from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate
from nautilus_trader.adapters.polymarket.sports import sports_data_type
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from src.arbitrage.common.control import TOPIC_ARBITRAGE_PARAMS
from src.arbitrage.common.control import SetArbitrageParamsCommand
from src.arbitrage.common.open_orders import pair_open_orders_digest
from src.arbitrage.common.opportunity import OpportunityMeta
from src.arbitrage.common.opportunity import CancelOpportunityMeta
from src.arbitrage.common.opportunity import cancel_params_from_meta
from src.arbitrage.common.opportunity import new_opportunity_id
from src.arbitrage.common.opportunity import tags_from_meta
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.common.params import ArbitrageParams
from src.arbitrage.common.positions import pair_positions_digest
from src.arbitrage.common.venues import is_probability_odds_venue
from src.arbitrage.matching.events import MatchedPair
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.condition import evaluate_tree
from src.arbitrage.strategy.registry import StrategyRegistry


def make_submitter(*, cache, order_factory, submit_order, log):
    """把 Action spec 转成 NT Order，再经 `Strategy.submit_order` 提交。

    `spec` schema:`{instrument_id, side: "BUY"|"SELL", qty: float, price: float, ...}`

    流程:`spec` → 经 `cache.instrument(iid).{size_precision,price_precision}` 构 NT `LimitOrder`
    → `Strategy.submit_order` 发布 initialized event、写 Cache、检查重复 ID并路由 RiskEngine
    → Risk pass 后再进 Execution opportunity barrier / venue ExecClient。

    `cache.instrument(iid)` 返 None(冷启动 / 未订阅)→ 跳过 + warning,不 raise。
    """
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.enums import TimeInForce
    from nautilus_trader.model.identifiers import ClientOrderId
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.objects import Price
    from nautilus_trader.model.objects import Quantity

    async def submit(spec: dict) -> None:
        raw_iid = spec["instrument_id"]
        iid = raw_iid if isinstance(raw_iid, InstrumentId) else InstrumentId.from_str(str(raw_iid))
        inst = cache.instrument(iid)
        if inst is None:
            log.warning(f"submit: instrument {iid} not in cache; skip")
            return
        side_str = spec.get("side", "BUY").upper()
        order_side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL
        tags = [f"arb:intent={spec.get('intent', 'arbitrage')}"]
        if all(k in spec for k in ("opportunity_id", "pair_id", "leg_key", "expected_legs")):
            tags = tags_from_meta(OpportunityMeta(
                opportunity_id=str(spec["opportunity_id"]),
                pair_id=str(spec["pair_id"]),
                leg_key=str(spec["leg_key"]),
                expected_legs=tuple(str(v) for v in spec["expected_legs"]),
                open_orders_digest=spec.get("open_orders_digest"),
                positions_digest=spec.get("positions_digest"),
                intent=str(spec.get("intent", "arbitrage")),
                venue_required_balance=spec.get("venue_required_balance"),
            ))
        order = order_factory.limit(
            instrument_id=iid,
            order_side=order_side,
            quantity=Quantity(float(spec["qty"]), precision=inst.size_precision),
            price=Price(float(spec["price"]), precision=inst.price_precision),
            time_in_force=TimeInForce.GTC,
            tags=tags,
            client_order_id=ClientOrderId(f"ARB-{UUID4().value[:8]}"),
        )
        submit_order(order)

    return submit


class StrategyEvaluatorConfig(StrategyConfig, frozen=True, kw_only=True):
    """单一运行时 evaluator strategy 的配置。"""

    # NT `Strategy.__init__` 的实际实现要求这里是 str（上游注解仍写 StrategyId）。
    strategy_id: StrategyId | None = "ARB-EVAL"
    order_id_tag: str | None = "001"
    log_evaluations: bool = False


@dataclass(slots=True)
class _RuntimeDeps:
    """非 msgspec config 可放对象的字段(launcher 经 ArbContext 注入)。"""

    pair_registry: PairRegistry
    strategy_registry: StrategyRegistry
    portfolio: object                      # ArbitragePortfolio
    is_execution_active: Callable[[], bool]  # Q19/§6.10:在飞跳过
    loop: object                            # 单测兜底 loop;生产调度使用 NT register_executor 注入的 ActorExecutor loop
    arbitrage_params: ArbitrageParams | None = None  # Web Arbitrage 运行时默认 share/max_leg_share
    pair_inflight: object = None            # PairInFlightGate(§7);None → 不串行(测试/降级)


class StrategyEvaluator(Strategy):
    def __init__(self, config: StrategyEvaluatorConfig, deps: _RuntimeDeps) -> None:
        super().__init__(config=config)
        self._pair_registry = deps.pair_registry
        self._strategy_registry = deps.strategy_registry
        self._portfolio = deps.portfolio
        self._is_execution_active = deps.is_execution_active
        self._arbitrage_params = deps.arbitrage_params or ArbitrageParams()
        self._test_loop = deps.loop
        self._registered_task_loop = None
        self._log_evaluations = config.log_evaluations
        self._obd_subscribed: set[str] = set()  # slice 10e:已订 OBD 的 instrument_id(去重)
        self._pair_inflight = deps.pair_inflight  # §7:per-pair **评估**串行闸(#261:不进执行段)
        self._sports_store = None                 # #250:SportsGameStateStore(lazy,注册后经 self.cache 建)
        self._sports_subscribed: set[int] = set()  # #250:已订 sports 状态的 gameId
        self._game_obd: dict[int, set[str]] = {}   # #250:gameId → 本 actor 订过的 OBD 腿(ended 全退)

    # ── 生命周期 ─────────────────────────────────────────────────────
    def register_executor(self, loop, executor) -> None:
        super().register_executor(loop, executor)
        self._registered_task_loop = loop

    def on_start(self) -> None:
        # slice 10d(#52)修 Gap A:`subscribe_data` 强制 SubscribeData cmd 路由,需 client_id/instrument_id;
        # custom 组件间事件用 `msgbus.subscribe(topic)` 直订。
        # NT publish_data 经 `data.{DataType.topic}` 发布,无 metadata 时带尾部 `*`(= `data.MatchedPair*`);
        # 订阅串必须带同款 `*`,否则精确串不匹配带星 publish topic → on_data 永不触发。
        self._msgbus.subscribe(
            topic=f"data.{MatchedPair.__name__}*",
            handler=self.on_data,
        )
        # #250:sports 订阅按 game 粒度,在 MatchedPair 到达时逐场发起(`_ensure_sports_subscribed`),
        # ended 分发完毕后退订;on_start 无全局 sports 订阅。
        self._msgbus.subscribe(topic=TOPIC_ARBITRAGE_PARAMS, handler=self._on_set_arbitrage_params_cmd)
        # #108:strategy⊥健康检查互斥(`_hc_running` + `health_check.*`)已退役 —— 旧理由是"健康检查 reload
        # **执行页**会撞下单",但执行页 reload 已迁 NT reconciliation;剩余 competition 页 reload 在另一张页、
        # 且 OE 下单是 page.evaluate(与焦点无关),不冲突。详见 synchronization §8.6 / refactor #108。
        # slice 10e:OBD 不在 on_start 预订(无 instrument-level 订阅可言)——改 **MatchedPair fire 后
        # per-iid `subscribe_order_book_deltas(iid)`**(`_ensure_obd_subscribed`),真实赔率进 cache;
        # 订阅的 OBD 由 NT 投到 `on_order_book_deltas` → `_route_eval`(OBD-driven 重评)。

    # ── data 入口(NT 路由)──────────────────────────────────────────
    def on_data(self, data) -> None:
        # 1. slice 10e:MatchedPair fire → per-iid 订阅 OBD,把真实赔率引进 cache。
        #    #250:同时按场订阅 sports 状态 + 记录该场的 OBD 腿(ended 时全退)
        if isinstance(data, MatchedPair):
            self._ensure_obd_subscribed(data)
            self._ensure_sports_subscribed(data)
        # 2. 路由评估(#250:sports 事件按 game_id 扇出全部注册 pair;其余单 pair 路由)
        if isinstance(data, SportsGameUpdate):
            self._route_eval_sports(data)
            return
        self._route_eval(data)

    def on_order_book_deltas(self, deltas) -> None:
        # slice 10e:OBD-driven 重评 —— 订阅的 OBD 由 NT 投到此回调;经 instrument_id→PairRegistry→pair_id 评估
        # 只让 OE/SE(decimal 赔率盘)的 OBD 触发机会评估;PM(概率盘)不驱动机会
        # (避免基于陈旧对侧价触发)。订阅**不动**、book 照常更新(NT 在本回调前已更新),
        # 这里仅跳过"触发评估"这一步 —— OE/SE 触发时仍读到 PM 的最新盘口。
        iid = getattr(deltas, "instrument_id", None)
        if iid is not None:
            try:
                if is_probability_odds_venue(iid.venue.value):
                    return
            except (KeyError, AttributeError):
                pass
        self._route_eval(deltas)

    def _route_eval(self, data) -> None:
        target = self._extract_evaluation_target(data)
        if target is None:
            return
        pair_id, sport, competition = target
        self._dispatch_eval(pair_id, sport, competition, event_name=type(data).__name__)

    def _route_eval_sports(self, update: SportsGameUpdate) -> None:
        """#250:sports 事件只负责唤醒+定位 —— `game_id` 反查全部已注册 pair,
        按确定性顺序逐 pair 调度(各自受 PairInFlightGate 约束,无 event 级全局锁);
        未注册 game no-op。评估时从 Store 读取当前状态,不直接信事件 payload。
        ended:分发完毕后释放本场全部订阅(sports + 各 pair 腿 OBD)→ 归零回收。"""
        for pair_id in sorted(self._pair_registry.pair_ids_for_game(update.game_id)):
            sport, competition = self._pair_scope(pair_id)
            self._dispatch_eval(pair_id, sport, competition, event_name=type(update).__name__)
        if update.ended:
            self._release_game_subscriptions(update.game_id)

    def _ensure_sports_subscribed(self, mp: MatchedPair) -> None:
        """#250:MatchedPair 到达时订该 pair 所属场的 sports 状态,并记录该场的 OBD 腿。

        gid 经 PairRegistry 反查(matching 注册先于发布,同步时序安全);无 gid 的 pair
        (无 sports 覆盖的赛事)静默跳过。
        """
        gid = self._pair_registry.game_id_for_pair(mp.pair_id)
        if gid is None:
            return
        self._game_obd.setdefault(gid, set()).update(mp.tradable_instrument_ids)
        if gid in self._sports_subscribed:
            return
        self._sports_subscribed.add(gid)
        try:
            self.subscribe_data(sports_data_type(gid), client_id=ClientId(SPORTS_CLIENT))
        except Exception as e:  # noqa: BLE001 — 订阅失败不挡 OBD 主链路;同场下个 MatchedPair 重试
            self._sports_subscribed.discard(gid)
            self._log.warning(f"sports subscribe game {gid} failed: {e!r}")

    def _release_game_subscriptions(self, game_id: int) -> None:
        """#250:比赛终局 —— 退订本场 sports 与各 pair 腿 OBD(与 matching 侧退订汇合后
        归零,NT 收尾 + 内存回收:sports Store 条目、OBD managed book)。"""
        from nautilus_trader.model.identifiers import InstrumentId

        if game_id in self._sports_subscribed:
            self._sports_subscribed.discard(game_id)
            try:
                self.unsubscribe_data(sports_data_type(game_id), client_id=ClientId(SPORTS_CLIENT))
            except Exception as e:  # noqa: BLE001
                self._log.warning(f"sports unsubscribe game {game_id} failed: {e!r}")
        for iid_str in sorted(self._game_obd.pop(game_id, set())):
            if iid_str not in self._obd_subscribed:
                continue
            self._obd_subscribed.discard(iid_str)
            try:
                self.unsubscribe_order_book_deltas(InstrumentId.from_str(iid_str))
            except Exception as e:  # noqa: BLE001 — 单腿退订失败不挡其它
                self._log.warning(f"OBD unsubscribe {iid_str} failed: {e!r}")

    def _pair_scope(self, pair_id: str) -> tuple[str | None, str | None]:
        """从该 pair 任一腿的 instrument.info 解析 (sport, competition)(策略 scope 查找用)。"""
        from nautilus_trader.model.identifiers import InstrumentId

        for iid_str in sorted(self._pair_registry.instrument_ids_for_pair(pair_id)):
            inst = self.cache.instrument(InstrumentId.from_str(iid_str))
            info = getattr(inst, "info", None) if inst is not None else None
            if info and (info.get("sport") or info.get("competition")):
                return info.get("sport"), info.get("competition")
        return None, None

    def _dispatch_eval(self, pair_id: str, sport, competition, *, event_name: str) -> None:
        # scope-priority 查策略(挂载存在锁定,Q21-a)
        strategy = self._strategy_registry.get_for(pair_id, competition, sport)
        if strategy is None:
            if self._log_evaluations:
                self._log.info(
                    f"Strategy evaluate skipped: pair_id={pair_id}, sport={sport}, "
                    f"competition={competition}, reason=no_strategy",
                )
            return
        if self._log_evaluations:
            self._log.info(
                f"Strategy evaluate scheduled: pair_id={pair_id}, sport={sport}, "
                f"competition={competition}, event={event_name}",
            )
        # §6.10 §7:per-pair 串行 —— 同步 acquire(`create_task` 前,首个 await 前)。
        # 同 pair 已在飞(评估中/执行中)→ 直接放弃,不派发评估。单 loop 串行保证同突发后到的评估立刻看到。
        if self._pair_inflight is not None and not self._pair_inflight.try_enter(pair_id):
            if self._log_evaluations:
                self._log.info(f"Strategy evaluate skipped: pair_id={pair_id}, reason=pair_in_flight")
            return
        # acquire 与 release 同层对称 —— 闸在此置位,由本次 task 的 done-callback 唯一释放。
        # #261:闸只管"同 pair 不并发评估";全局 ≤1 执行由 barrier 判定,strategy 不参与。
        coro = self._evaluate_and_fire(strategy, pair_id)
        try:
            task = self._create_task(coro)
        except Exception:
            # 排程失败 → 协程**从未被排程**,一定不会跑、不会下单 → 此处释放闸是安全的。
            # (对比:若协程已排程仅回调挂不上,释放才会造成"闸放了但还在下单",故不在别处这么做。)
            coro.close()
            if self._pair_inflight is not None:
                self._pair_inflight.release(pair_id)
            raise                                     # 不吞:排程失败是真故障,要响亮暴露
        task.add_done_callback(partial(self._on_eval_done, pair_id))

    def _on_eval_done(self, pair_id: str, task) -> None:
        """评估 task 的**唯一**闸出口(#261:无条件释放)。

        闸只保证"同一 pair 不并发评估",生命周期 = 本 task 的生命周期,故正常返回 / 抛异常 /
        被取消都释放,没有"已 fire 就交给执行"的例外 —— 那条例外正是 #260 泄漏的根源
        (跨组件交接需要判据,判据会漏)。全局 ≤1 执行由 barrier 用派生态另行保证。
        """
        if not task.cancelled() and (exc := task.exception()) is not None:
            # fire-and-forget task 的异常否则会被静默吞掉(只在 GC 时冒一条无上下文的 asyncio 警告)。
            # NT `Logger.error` 不吃 `exc_info`,故手工展开 traceback —— 异常可能来自 action 链任意
            # 深处(见 `web/actor.py:_on_serve_done` 的 `{exc!r}`:那里失败点唯一,这里需要栈定位)。
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            self._log.error(f"Strategy evaluate failed: pair_id={pair_id}\n{tb}")
        if self._pair_inflight is not None:
            self._pair_inflight.release(pair_id)

    def _ensure_obd_subscribed(self, mp: MatchedPair) -> None:
        """slice 10e:MatchedPair 的两边各腿首次见到时订阅 OrderBookDeltas(去重)。
        instrument 已在 cache(slice A 发现);OE/PM data client `_subscribe_order_book_deltas` 接 WS 流。"""
        from nautilus_trader.model.identifiers import InstrumentId

        instrument_ids = list(mp.tradable_instrument_ids)
        for iid_str in instrument_ids:
            if iid_str in self._obd_subscribed:
                continue
            self._obd_subscribed.add(iid_str)
            try:
                # 概率校验通路已由 Matching 建好 managed book；这里只加入现有 feed，避免
                # DataEngine 再建空 OrderBook 覆盖 cache 首帧。关闭校验时仍由 Strategy 建 book。
                self.subscribe_order_book_deltas(
                    InstrumentId.from_str(iid_str),
                    managed=not mp.order_books_managed,
                )
            except Exception as e:  # noqa: BLE001 — 单腿订阅失败不挡其它
                self._log.warning(f"OBD subscribe {iid_str} failed: {e!r}")

    # ── 评估主流程 ────────────────────────────────────────────────────
    async def _evaluate_and_fire(self, strategy, pair_id: str) -> None:
        """本方法**不碰 pair 闸** —— 闸由 `_dispatch_eval` 置位、`_on_eval_done` 无条件释放。"""
        # Q19:执行在飞 → 直接让路(策略前置 pre-check 放弃机会)
        if self._is_execution_active():
            if self._log_evaluations:
                self._log.info(f"Strategy evaluate skipped: pair_id={pair_id}, reason=execution_active")
            return
        instrument_ids = self._pair_registry.instrument_ids_for_pair(pair_id)
        open_orders_digest = pair_open_orders_digest(self.cache, instrument_ids)
        positions_digest = pair_positions_digest(self.cache, instrument_ids)
        sports_store = self._get_sports_store()
        # 套利树 / 补偿树必须各自持有独立 scratch:Check 会把 legs 写入 scratch 给同树 Action 消费,
        # 若两树共用 ctx,补偿树单腿会覆盖套利树双腿。
        submitter = self._make_submitter()
        base_ctx = {
            "pair_id": pair_id,
            "cache": self.cache,
            "pair_registry": self._pair_registry,
            "sports_store": sports_store,
            "open_orders_digest": open_orders_digest,
            "positions_digest": positions_digest,
            "submitter": submitter,
            "pair_order_canceler": self._make_pair_order_canceler(),
            "portfolio": self._portfolio,
            "strategy_defaults": self._strategy_defaults(),
        }
        arb_ctx = EvalContext(**base_ctx)
        comp_ctx = EvalContext(**base_ctx)
        # 并行 evaluate;各树 scratch 独立,顶层等两树都返回后再决定 fire
        arb_res, comp_res = await asyncio.gather(
            self._aevaluate(strategy.arbitrage_tree, arb_ctx),
            self._aevaluate(strategy.compensation_tree, comp_ctx),
        )
        if self._log_evaluations:
            arb_actions_str = [type(a).__name__ for a in arb_res.pending_actions] if arb_res.pending_actions else None
            comp_actions_str = [type(a).__name__ for a in comp_res.pending_actions] if comp_res.pending_actions else None
            self._log.info(
                f"Strategy evaluate result: pair_id={pair_id}, arb_hit={arb_res.hit}, "
                f"arb_actions={arb_actions_str}, "
                f"comp_hit={comp_res.hit}, "
                f"comp_actions={comp_actions_str}",
            )
        # 树间取舍下移至 candi_select。顶层只决定跑哪条链:
        # arb 命中 → 跑 arb 链,comp 同轮命中时把 comp legs 包成 recovery candidate 注入
        # (candi_select 先选通过门控的补偿候选,补偿全灭才落套利);仅 comp 命中 → comp 链。
        # `await` 而非 `create_task`:让 action 链的异常落进本 task,由 `_on_eval_done` 打出来,
        # 而不是变成无上下文的 asyncio "Task exception was never retrieved"。
        if arb_res.hit and arb_res.pending_actions:
            if comp_res.hit and comp_res.pending_actions and comp_ctx.scratch.get("legs"):
                recovery_candidate = {
                    "candidate_id": "recovery",
                    "intent": "recovery",
                    "legs": comp_ctx.scratch["legs"],
                }
                cancel_request = comp_ctx.scratch.get("cancel_pair_orders")
                if cancel_request:
                    recovery_candidate["cancel_pair_orders"] = cancel_request
                arb_ctx.scratch["recovery_candidates"] = [recovery_candidate]
            if self._log_evaluations:
                self._log.info(f"Strategy action fired: pair_id={pair_id}, action=arbitrage")
            await self._execute_actions(arb_res.pending_actions, arb_ctx)
        elif comp_res.hit and comp_res.pending_actions:
            if self._log_evaluations:
                self._log.info(f"Strategy action fired: pair_id={pair_id}, action=compensation")
            await self._execute_actions(comp_res.pending_actions, comp_ctx)
        elif self._log_evaluations:
            self._log.info(f"Strategy action skipped: pair_id={pair_id}, reason=no_pending_actions")

    def _get_sports_store(self):
        """#250:lazy 建 SportsGameStateStore，供状态查询读取 PMS Cache。"""
        if self._sports_store is None:
            try:
                self._sports_store = SportsGameStateStore(self.cache)
            except Exception:  # noqa: BLE001 — 未注册 harness 无 cache
                return None
        return self._sports_store

    def _create_task(self, coro):
        """把 coroutine 投递到 NT kernel 为组件注册的运行 loop。

        loop 身份由 NT 保证:`kernel.py:1016` 在 `start_async()`(已跑在 kernel loop 上)内调
        `_register_executor()` → `register_executor(self._loop, ...)`,故 `_registered_task_loop`
        就是真正运行中的那个 loop。`_test_loop` 只用于未注册 executor 的单测 harness。

        **#260:删除了 `call_soon_threadsafe` 兜底分支。** 它防的是"当前不在该 loop 上"(与 f017b78ee0
        那次"loop 身份取错 → 协程静默不跑"是两个不同问题,旧 docstring 把二者混写)。该分支返回
        `Handle` 而非 Task,挂不了 done-callback,会让闸的释放静默失效;且它把"协程会不会跑"变成
        模糊态。按 #105 ② 同款纪律(无兜底猜测):不预防未证实的情况,真发生就让它响亮抛
        `RuntimeError: Non-thread-safe operation ...`,届时按实际成因解决。
        """
        loop = self._registered_executor_loop()
        if loop is None:
            return self._test_loop.create_task(coro)
        return loop.create_task(coro)

    def _registered_executor_loop(self):
        return self._registered_task_loop

    async def _aevaluate(self, tree, ctx):
        """async 包 sync 的 `evaluate_tree`,使 `asyncio.gather` 模式可用。
        Check 实现层若需要 async I/O(查外部服务),可演进为真正 async 求值;现框架是 sync。"""
        return evaluate_tree(tree, ctx)

    async def _execute_actions(self, actions: list, ctx) -> None:
        """依次执行 actions(串行,保证顺序;如 ShareLimitModification → PlaceBetsAction)。"""
        for action in actions:
            await action.execute(ctx)

    def _strategy_defaults(self) -> dict:
        params = self._arbitrage_params
        return {
            "share": params.share,
            "max_leg_share": params.max_leg_share,
        }

    def _on_set_arbitrage_params_cmd(self, cmd) -> None:
        if not isinstance(cmd, SetArbitrageParamsCommand):
            return
        overrides = {
            k: v
            for k, v in (
                ("share", cmd.share),
                ("max_leg_share", cmd.max_leg_share),
                ("fx", cmd.fx),
            )
            if v is not None
        }
        if overrides:
            self._arbitrage_params = replace(self._arbitrage_params, **overrides)

    # ── slice 10a(#50):submitter 工厂 ───────────────────────────────
    def _make_submitter(self):
        """返 async callable:Action 经 `await ctx.submitter(spec)` 提交订单。

        thin wrapper 经 module-level `make_submitter(...)`,最终调用 NT 原生 `Strategy.submit_order`。
        """
        return make_submitter(
            cache=self.cache,
            order_factory=self.order_factory,
            submit_order=self.submit_order,
            log=self._log,
        )

    def _make_pair_order_canceler(self):
        """返同步 callable：重读 pair open orders，并作为同组 NT CancelOrder 送入 barrier。"""
        def cancel(pair_id: str) -> int:
            seen = set()
            orders = []
            for instrument_id in sorted(
                self._pair_registry.instrument_ids_for_pair(pair_id),
                key=str,
            ):
                for order in self.cache.orders_open(instrument_id=instrument_id) or ():
                    key = str(getattr(order, "client_order_id", "") or id(order))
                    if key in seen:
                        continue
                    seen.add(key)
                    orders.append(order)
            expected = tuple(
                str(getattr(order, "client_order_id", "") or "")
                for order in orders
            )
            opportunity_id = new_opportunity_id()
            for order in orders:
                cancel_key = str(getattr(order, "client_order_id", "") or "")
                self.cancel_order(
                    order,
                    params=cancel_params_from_meta(
                        CancelOpportunityMeta(
                            opportunity_id=opportunity_id,
                            pair_id=pair_id,
                            cancel_key=cancel_key,
                            expected_cancels=expected,
                        ),
                    ),
                )
            return len(orders)

        return cancel

    # ── 提取评估目标(支持 OrderBookDeltas / MatchedPair / 其它)──────
    def _extract_evaluation_target(self, data) -> tuple[str, str | None, str | None] | None:
        """返 (pair_id, sport, competition) 或 None(跳过该 event)。"""
        # MatchedPair:直接带 pair_id/sport/competition
        if isinstance(data, MatchedPair):
            return (data.pair_id, data.sport, data.competition)
        # 通用:经 instrument_id → PairRegistry → pair_id;sport/comp 从 instrument.info
        iid = getattr(data, "instrument_id", None)
        if iid is None:
            return None
        pair_id = self._pair_registry.get(iid)
        if pair_id is None:
            return None
        inst = self.cache.instrument(iid)
        if inst is None or not getattr(inst, "info", None):
            return (pair_id, None, None)
        return (pair_id, inst.info.get("sport"), inst.info.get("competition"))
