"""
Matching 自定义 Data 事件 —— `MatchedPair`。

由 `MarketMatchingActor` 在成功匹配 anchor↔tradable 事件后 publish;Strategy 订阅决策。
**risk/portfolio/session 不订此事件**——它们经 `PairRegistry` pull 拿 pair_id(注册与发布
同 tick 内由 matching 完成,语义同步)。

走 NT `@customdataclass`(`nautilus_trader.model.custom`)注册为可发布 Data 类型 +
可序列化(arrow + serializer)。同 `InstrumentsRefreshed` 模式。
"""

from __future__ import annotations

from dataclasses import field
from typing import Any

import pyarrow as pa

from nautilus_trader.core import Data
from nautilus_trader.model.custom import customdataclass

@customdataclass
class MatchedPair(Data):
    """跨 venue 匹配上的一个事件 + 各 venue 方向腿的 instrument_id。

    Fields(`@customdataclass` 自动注入 `ts_event` / `ts_init`):
    - `pair_id`           稳定 ID(matching 算出,见架构 §4.3)
    - `sport`             标准化 sport
    - `competition`       联赛名(**不是** pair_id,#34)
    - `anchor_instrument_ids` 非交易 anchor 腿(PMSPORTS 等),不参与套利
    - `tradable_instrument_ids` 所有可交易腿,供 strategy/risk 使用
    - `venue_instrument_ids` 按 venue 分组的可交易腿
    - `confidence`        队名相似度归一(0-1)
    """

    pair_id: str
    sport: str
    competition: str
    confidence: float
    anchor_instrument_ids: list[str] = field(default_factory=list)
    tradable_instrument_ids: list[str] = field(default_factory=list)
    venue_instrument_ids: dict[str, list[str]] = field(default_factory=dict)

    _schema = pa.schema(
        {
            "pair_id": pa.string(),
            "sport": pa.string(),
            "competition": pa.string(),
            "anchor_instrument_ids": pa.list_(pa.string()),
            "tradable_instrument_ids": pa.list_(pa.string()),
            "venue_instrument_ids": pa.map_(pa.string(), pa.list_(pa.string())),
            "confidence": pa.float64(),
            "ts_event": pa.int64(),
            "ts_init": pa.int64(),
        },
        metadata={"type": "MatchedPair"},
    )

    def __post_init__(self) -> None:
        if not self.tradable_instrument_ids and self.venue_instrument_ids:
            self.tradable_instrument_ids = _flatten_venue_ids(self.venue_instrument_ids)

    def to_dict(self, to_arrow: bool = False) -> dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "sport": self.sport,
            "competition": self.competition,
            "anchor_instrument_ids": list(self.anchor_instrument_ids),
            "tradable_instrument_ids": list(self.tradable_instrument_ids),
            "venue_instrument_ids": {
                str(venue).upper(): list(ids)
                for venue, ids in (self.venue_instrument_ids or {}).items()
            },
            "confidence": self.confidence,
            "ts_event": self.ts_event,
            "ts_init": self.ts_init,
        }

    @staticmethod
    def from_dict(values: dict[str, Any]) -> "MatchedPair":
        venue_ids = _normalize_venue_instrument_ids(values.get("venue_instrument_ids"))
        tradable_ids = list(
            values.get("tradable_instrument_ids")
            or _flatten_venue_ids(venue_ids)
        )
        anchor_ids = list(values.get("anchor_instrument_ids") or [])
        return MatchedPair(
            ts_event=values["ts_event"],
            ts_init=values["ts_init"],
            pair_id=values["pair_id"],
            sport=values["sport"],
            competition=values["competition"],
            confidence=float(values["confidence"]),
            anchor_instrument_ids=anchor_ids,
            tradable_instrument_ids=tradable_ids,
            venue_instrument_ids=venue_ids,
        )


def _normalize_venue_instrument_ids(value: Any) -> dict[str, list[str]]:
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(venue).upper(): list(ids) for venue, ids in value.items()}
    return {str(venue).upper(): list(ids) for venue, ids in list(value)}


def _flatten_venue_ids(venue_ids: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for ids in venue_ids.values():
        out.extend(ids)
    return out
