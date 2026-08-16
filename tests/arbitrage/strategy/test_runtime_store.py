"""StrategyRuntimeStore 单元测试。"""

from src.arbitrage.strategy.runtime_store import StrategyRuntimeStore


def test_variables_are_isolated_by_strategy_and_pair():
    store = StrategyRuntimeStore()

    store.update("head_rebate", "pair-a", {"standard": 0.1})
    store.update("head_rebate", "pair-b", {"standard": 0.2})
    store.update("other", "pair-a", {"standard": 0.3})

    assert store.get("head_rebate", "pair-a", "standard") == 0.1
    assert store.get("head_rebate", "pair-b", "standard") == 0.2
    assert store.get("other", "pair-a", "standard") == 0.3


def test_update_merges_pair_variables_and_returns_copy():
    store = StrategyRuntimeStore()

    store.update("head_rebate", "pair-a", {"standard": 0.1, "mode": "reverse"})
    updated = store.update("head_rebate", "pair-a", {"standard": 0.2})
    updated["standard"] = 9.9

    assert store.variables("head_rebate", "pair-a") == {
        "standard": 0.2,
        "mode": "reverse",
    }


def test_reads_and_writes_do_not_expose_nested_mutable_values():
    store = StrategyRuntimeStore()
    source = {"rates": {"yes": 0.1}}

    store.update("head_rebate", "pair-a", source)
    source["rates"]["yes"] = 9.9
    returned = store.variables("head_rebate", "pair-a")
    returned["rates"]["yes"] = 8.8

    assert store.get("head_rebate", "pair-a", "rates") == {"yes": 0.1}


def test_delete_pair_only_removes_target_pair_and_prunes_empty_strategy():
    store = StrategyRuntimeStore()
    store.update("head_rebate", "pair-a", {"standard": 0.1})
    store.update("head_rebate", "pair-b", {"standard": 0.2})

    store.delete_pair("head_rebate", "pair-a")

    assert store.variables("head_rebate", "pair-a") == {}
    assert store.get("head_rebate", "pair-b", "standard") == 0.2

    store.delete_pair("head_rebate", "pair-b")
    assert store.snapshot() == {}


def test_delete_strategy_removes_all_its_pairs_only():
    store = StrategyRuntimeStore()
    store.update("head_rebate", "pair-a", {"standard": 0.1})
    store.update("head_rebate", "pair-b", {"standard": 0.2})
    store.update("other", "pair-a", {"standard": 0.3})

    store.delete_strategy("head_rebate")

    assert store.variables("head_rebate", "pair-a") == {}
    assert store.get("other", "pair-a", "standard") == 0.3


def test_missing_value_returns_copied_default():
    store = StrategyRuntimeStore()
    default = {"standard": None}

    returned = store.get("head_rebate", "pair-a", "missing", default)
    returned["standard"] = 0.1

    assert default == {"standard": None}


def test_delete_pair_from_all_strategies():
    store = StrategyRuntimeStore()
    store.update("head_rebate", "pair-a", {"standard": 0.1})
    store.update("other", "pair-a", {"standard": 0.2})
    store.update("other", "pair-b", {"standard": 0.3})

    store.delete_pair_from_all_strategies("pair-a")

    assert store.variables("head_rebate", "pair-a") == {}
    assert store.variables("other", "pair-a") == {}
    assert store.get("other", "pair-b", "standard") == 0.3
