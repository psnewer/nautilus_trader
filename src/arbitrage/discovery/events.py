"""
Discovery 自定义 Data 事件 —— `InstrumentsRefreshed`。

由 `InstrumentRefresher` 在一轮 `provider.load_all_async()` 成功 + count>0 后 publish;
`MarketMatchingActor` 订阅两 venue 的 `InstrumentsRefreshed` 做"两家近期都刷新"门控(Q4/Q5)。

走 NT `@customdataclass`(`nautilus_trader.model.custom`)注册为可发布 Data 类型 +
可序列化(`register_serializable_type` + arrow);msgbus pub/sub 直通。
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from nautilus_trader.core import Data
from nautilus_trader.model.custom import customdataclass


@customdataclass
class InstrumentsRefreshed(Data):
    """一个 venue 一轮 instrument 刷新成功的标记事件。

    Fields(`@customdataclass` 自动注入 `ts_event` / `ts_init`):
    - `venue` "POLYMARKET" / "ORBITEXCH"(消费方按 venue gate)
    - `count` 本轮成功落库的 instrument 数(0 时调用方应不 publish)
    """

    venue: str
    count: int

    _schema = pa.schema(
        {
            "venue": pa.string(),
            "count": pa.int64(),
            "ts_event": pa.int64(),
            "ts_init": pa.int64(),
        },
        metadata={"type": "InstrumentsRefreshed"},
    )

    def to_dict(self, to_arrow: bool = False) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "count": self.count,
            "ts_event": self.ts_event,
            "ts_init": self.ts_init,
        }

    @staticmethod
    def from_dict(values: dict[str, Any]) -> "InstrumentsRefreshed":
        return InstrumentsRefreshed(
            ts_event=values["ts_event"],
            ts_init=values["ts_init"],
            venue=values["venue"],
            count=values["count"],
        )
