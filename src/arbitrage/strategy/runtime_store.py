"""StrategyRuntimeStore —— 按策略与 pair 隔离的跨轮运行时变量。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy


class StrategyRuntimeStore:
    """
    保存 Strategy 组件拥有的进程内运行时变量。

    第一层按 strategy_id 隔离,第二层按 pair_id 隔离。同一 pair 的多个变量通过
    `update` 一次写入;所有读写都复制值,调用方不能绕过 Store 修改内部状态。
    """

    def __init__(self) -> None:
        self._values: dict[str, dict[str, dict[str, object]]] = {}

    def variables(self, strategy_id: str, pair_id: str) -> dict[str, object]:
        """返回指定策略与 pair 的变量副本;不存在时返回空字典。"""
        values = self._values.get(strategy_id, {}).get(pair_id, {})
        return deepcopy(values)

    def get(
        self,
        strategy_id: str,
        pair_id: str,
        name: str,
        default: object = None,
    ) -> object:
        """读取单个变量;不存在时返回 `default` 的副本。"""
        values = self._values.get(strategy_id, {}).get(pair_id)
        if values is None or name not in values:
            return deepcopy(default)
        return deepcopy(values[name])

    def update(
        self,
        strategy_id: str,
        pair_id: str,
        values: Mapping[str, object],
    ) -> dict[str, object]:
        """一次合并写入同一策略与 pair 的多个变量,并返回更新后的副本。"""
        pair_values = self._values.setdefault(strategy_id, {}).setdefault(pair_id, {})
        pair_values.update(deepcopy(dict(values)))
        return deepcopy(pair_values)

    def delete_pair(self, strategy_id: str, pair_id: str) -> None:
        """删除一个策略在指定 pair 上的全部运行时变量。"""
        strategy_values = self._values.get(strategy_id)
        if strategy_values is None:
            return
        strategy_values.pop(pair_id, None)
        if not strategy_values:
            self._values.pop(strategy_id, None)

    def delete_strategy(self, strategy_id: str) -> None:
        """删除一个策略的全部运行时变量。"""
        self._values.pop(strategy_id, None)

    def delete_pair_from_all_strategies(self, pair_id: str) -> None:
        """比赛结束时从所有策略中删除指定 pair 的变量。"""
        for strategy_id in tuple(self._values):
            self.delete_pair(strategy_id, pair_id)

    def snapshot(self) -> dict[str, dict[str, dict[str, object]]]:
        """返回整个 Store 的诊断副本。"""
        return deepcopy(self._values)
