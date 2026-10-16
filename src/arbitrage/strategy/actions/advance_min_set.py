"""
命中撤平仓单分支时，把本 pair 对应的止盈/止损盘数阈值推进到 current_set+offset。

替代原先硬编码在 actor `OrderCanceled` 回调里的自动写入:放在补偿树"针对仓位挂单撤单"分支的
action 链里,在该分支命中(决定撤单)时写入。订单身份(origin=in_game、针对仓位的 SELL
平仓单)由分支的 `exposure_source(position_order=true)` 保证,本 Action 只按
当前盘数推进阈值;
盘数不可判定(current_set<=0)时不写。
"""

from __future__ import annotations

from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.in_game_positions import current_set_number


class _AdvanceSetThresholdAction(Action):
    """两个公开 Action 共用的盘数读取与校验。"""

    action_name = "advance_set_threshold"

    def __init__(self, offset: int = 1) -> None:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset <= 0:
            raise ValueError(f"{self.action_name}: offset must be a positive integer")
        self._offset = offset

    async def execute(self, ctx: EvalContext) -> None:
        if ctx.in_game_position_store is None or ctx.pair_registry is None:
            return
        current_set = current_set_number(
            pair_id=ctx.pair_id,
            pair_registry=ctx.pair_registry,
            sports_store=ctx.sports_store,
        )
        if current_set <= 0:
            return
        self._write(ctx, current_set + self._offset)

    def _write(self, ctx: EvalContext, value: int) -> None:
        raise NotImplementedError


class AdvanceMinTpSetAction(_AdvanceSetThresholdAction):
    """推进本 pair 的止盈最小盘数。"""

    action_name = "advance_min_tp_set"

    def _write(self, ctx: EvalContext, value: int) -> None:
        ctx.in_game_position_store.set_min_tp_set(ctx.pair_id, value)


class AdvanceMinSlSetAction(_AdvanceSetThresholdAction):
    """推进本 pair 的止损最小盘数。"""

    action_name = "advance_min_sl_set"

    def _write(self, ctx: EvalContext, value: int) -> None:
        ctx.in_game_position_store.set_min_sl_set(ctx.pair_id, value)
