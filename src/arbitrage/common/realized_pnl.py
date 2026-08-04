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
        self._account_revisions: dict[str, int] = {}

    def replace_instrument_snapshot(
        self,
        account_id,
        *,
        external_realized: dict[str, float],
        native_realized: dict[str, float],
        only_instruments=None,
    ) -> None:
        """更新该账户的 realized offset 基线。

        `only_instruments=None`:整账户全量替换(旧语义,删该账户全部 offset 再按 external 重建)。
        `only_instruments`(#318):**per-instrument 选择性更新** —— 只更新给定 instrument 的 offset
        (通过 per-pair 校验的那些),其余 instrument 的 offset **原样保留**。offset 公式不变:
        `external(fetch) - native(now)`。
        """
        account = str(account_id)
        if only_instruments is None:
            stale = [key for key in self._instrument_offsets if key[0] == account]
            for key in stale:
                del self._instrument_offsets[key]
            targets = [str(instrument_id) for instrument_id in external_realized]
        else:
            targets = [str(instrument_id) for instrument_id in only_instruments]

        for instrument_id in targets:
            offset = float(external_realized.get(instrument_id, 0.0)) - float(
                native_realized.get(instrument_id, 0.0),
            )
            key = (account, instrument_id)
            if abs(offset) > 1e-12:
                self._instrument_offsets[key] = offset
            else:
                self._instrument_offsets.pop(key, None)
        self._account_revisions[account] = self._account_revisions.get(account, 0) + 1

    def revision(self, account_id) -> int:
        """返回账户账本的单调版本，供 reconciliation 乐观并发校验。"""
        return self._account_revisions.get(str(account_id), 0)

    def instrument_adjustment(self, instrument_id, account_id=None) -> float:
        instrument = str(instrument_id)
        if account_id is not None:
            return self._instrument_offsets.get((str(account_id), instrument), 0.0)
        return sum(
            value
            for (account, stored_instrument), value in self._instrument_offsets.items()
            if stored_instrument == instrument
        )
