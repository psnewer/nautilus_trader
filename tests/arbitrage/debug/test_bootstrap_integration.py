"""Q11 Debug 框架与 bootstrap/install_arbitrage_engines 的接线。

对应用例:debug-bootstrap.{1-4}
"""

import nautilus_trader.system.kernel as _kernel
import pytest

import src.arbitrage.bootstrap as bootstrap
from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.debug.config import DebugOverride
from src.arbitrage.debug.risk import DebugArbitrageLiveRiskEngine
from src.arbitrage.risk.engine import ArbitrageLiveRiskEngine


@pytest.fixture(autouse=True)
def _restore_kernel_types():
    """每个 test 后还原 kernel 的类替换,避免污染。"""
    orig_portfolio = _kernel.Portfolio
    orig_engine = _kernel.LiveRiskEngine
    yield
    _kernel.Portfolio = orig_portfolio
    _kernel.LiveRiskEngine = orig_engine


# ── debug-bootstrap.1: debug_config=None → 装生产 ArbitrageLiveRiskEngine ─
def test_install_without_debug_uses_production_engine():
    bootstrap.install_arbitrage_engines()  # 无参 = production
    assert _kernel.LiveRiskEngine is ArbitrageLiveRiskEngine


# ── debug-bootstrap.2: debug_config.enabled=False → 装生产 ────────
def test_install_with_disabled_debug_uses_production_engine():
    cfg = DebugConfig(enabled=False)
    cfg.overrides["skip_check_size"] = DebugOverride(name="skip_check_size", enabled=True, value=True)
    bootstrap.install_arbitrage_engines(debug_config=cfg)
    assert _kernel.LiveRiskEngine is ArbitrageLiveRiskEngine


# ── debug-bootstrap.3: debug_config.enabled=True → 装 Debug 子类(继承 production)──
def test_install_with_enabled_debug_uses_debug_engine_subclass():
    cfg = DebugConfig(enabled=True)
    cfg.overrides["skip_check_size"] = DebugOverride(name="skip_check_size", enabled=True, value=True)
    bootstrap.install_arbitrage_engines(debug_config=cfg)
    installed = _kernel.LiveRiskEngine
    # 是 Debug 子类(同时也是 production 子类,wire 的 isinstance 检查仍过)
    assert installed is not ArbitrageLiveRiskEngine
    assert issubclass(installed, DebugArbitrageLiveRiskEngine)
    assert issubclass(installed, ArbitrageLiveRiskEngine)


# ── debug-bootstrap.4: kernel-injected 包装类内闭包绑住 debug_config ─
def test_kernel_injected_wrapper_closes_over_debug_config():
    """kernel 不会传 debug=,包装类 __init__ 从闭包注入。验证:不同 cfg 装出来的包装类不同。"""
    cfg_a = DebugConfig(enabled=True)
    cfg_a.overrides["a"] = DebugOverride(name="a", enabled=True, value=1)
    bootstrap.install_arbitrage_engines(debug_config=cfg_a)
    cls_a = _kernel.LiveRiskEngine

    cfg_b = DebugConfig(enabled=True)
    cfg_b.overrides["b"] = DebugOverride(name="b", enabled=True, value=2)
    bootstrap.install_arbitrage_engines(debug_config=cfg_b)
    cls_b = _kernel.LiveRiskEngine

    # 两次 install 装的是不同的包装类(各自闭包)
    assert cls_a is not cls_b
    # 两者都是 DebugArbitrageLiveRiskEngine 子类
    assert issubclass(cls_a, DebugArbitrageLiveRiskEngine)
    assert issubclass(cls_b, DebugArbitrageLiveRiskEngine)


# ── ArbContext.debug_config 字段存在,默认 None ──────────────────
def test_arb_context_has_debug_config_field_default_none():
    bootstrap.reset_arb_context()
    ctx = bootstrap.get_arb_context()
    assert hasattr(ctx, "debug_config")
    assert ctx.debug_config is None
    # prepare 可塞 debug_config
    cfg = DebugConfig(enabled=True)
    ctx2 = bootstrap.prepare_arb_context(debug_config=cfg)
    assert ctx2.debug_config is cfg
    bootstrap.reset_arb_context()
