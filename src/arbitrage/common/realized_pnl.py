"""跨组件共享的已实现盈亏调整账本。

NT 继续负责当前进程内真实 Fill 产生的 instrument realized PnL；本账本只保存
PM Data API 对账值与 NT 当前值之间的 instrument 基线差。

Portfolio 按 PairRegistry 聚合 NT realized PnL 与本账本调整，避免伪造 OrderFilled。
"""

from __future__ import annotations


class RealizedPnlLedger:
    """单进程共享账本；运行时均在 NT app loop 上访问，无需加锁。"""

    def __init__(self) -> None:
        self._instrument_offsets: dict[tuple[str, str], float] = {}

    def replace_instrument_snapshot(
        self,
        account_id,
        *,
        external_realized: dict[str, float],
        native_realized: dict[str, float],
    ) -> None:
        """用一次完整 reconcile 快照替换该账户的基线。"""
        account = str(account_id)
        stale = [key for key in self._instrument_offsets if key[0] == account]
        for key in stale:
            del self._instrument_offsets[key]

        for instrument_id, external_value in external_realized.items():
            offset = float(external_value) - float(native_realized.get(instrument_id, 0.0))
            if abs(offset) > 1e-12:
                self._instrument_offsets[(account, str(instrument_id))] = offset

    def instrument_adjustment(self, instrument_id, account_id=None) -> float:
        instrument = str(instrument_id)
        if account_id is not None:
            return self._instrument_offsets.get((str(account_id), instrument), 0.0)
        return sum(
            value
            for (account, stored_instrument), value in self._instrument_offsets.items()
            if stored_instrument == instrument
        )
