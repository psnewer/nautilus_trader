"""
Matching 自定义 Data 事件 —— `MatchedPair`。

由 `MarketMatchingActor` 在成功匹配一对 PM↔OE 事件后 publish;Strategy 订阅决策。
**risk/portfolio/session 不订此事件**——它们经 `PairRegistry` pull 拿 pair_id(注册与发布
同 tick 内由 matching 完成,语义同步)。

走 NT `@customdataclass`(`nautilus_trader.model.custom`)注册为可发布 Data 类型 +
可序列化(arrow + serializer)。同 `InstrumentsRefreshed` 模式。
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from nautilus_trader.core import Data
from nautilus_trader.model.custom import customdataclass


@customdataclass
class MatchedPair(Data):
    """跨 PM/OE 匹配上的一对事件 + 两边各方向腿的 instrument_id。

    Fields(`@customdataclass` 自动注入 `ts_event` / `ts_init`):
    - `pair_id`           稳定 ID(matching 算出,见架构 §4.3)
    - `sport`             标准化 sport
    - `competition`       联赛名(**不是** pair_id,#34)
    - `pm_instrument_ids` PM 侧各 selection_role 的腿 instrument_id 字符串列表
    - `oe_instrument_ids` OE 侧同
    - `confidence`        队名相似度归一(0-1)
    """

    pair_id: str
    sport: str
    competition: str
    pm_instrument_ids: list[str]
    oe_instrument_ids: list[str]
    confidence: float

    _schema = pa.schema(
        {
            "pair_id": pa.string(),
            "sport": pa.string(),
            "competition": pa.string(),
            "pm_instrument_ids": pa.list_(pa.string()),
            "oe_instrument_ids": pa.list_(pa.string()),
            "confidence": pa.float64(),
            "ts_event": pa.int64(),
            "ts_init": pa.int64(),
        },
        metadata={"type": "MatchedPair"},
    )

    def to_dict(self, to_arrow: bool = False) -> dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "sport": self.sport,
            "competition": self.competition,
            "pm_instrument_ids": list(self.pm_instrument_ids),
            "oe_instrument_ids": list(self.oe_instrument_ids),
            "confidence": self.confidence,
            "ts_event": self.ts_event,
            "ts_init": self.ts_init,
        }

    @staticmethod
    def from_dict(values: dict[str, Any]) -> "MatchedPair":
        return MatchedPair(
            ts_event=values["ts_event"],
            ts_init=values["ts_init"],
            pair_id=values["pair_id"],
            sport=values["sport"],
            competition=values["competition"],
            pm_instrument_ids=list(values["pm_instrument_ids"]),
            oe_instrument_ids=list(values["oe_instrument_ids"]),
            confidence=float(values["confidence"]),
        )
