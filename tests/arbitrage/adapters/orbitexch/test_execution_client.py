"""
OrbitExchExecutionClient 测试(占位,详细用例待 Step 5 启动时展开)。

锁定决定(Step 5 实施前必须满足):
- 继承 NT LiveExecutionClient
- 必须实现 NT 契约: _submit_order / _cancel_order / _modify_order / _query_order
- 必须事件回写: generate_order_submitted / _filled / _rejected / _canceled
- 通过 manager.get_page("execution") 提交订单
- Reconciliation: generate_order_status_reports / generate_position_status_reports

对应章节: refactor.md §5.5
"""

import pytest


@pytest.mark.skip(reason="not implemented; pending Step 5")
def test_exec_client_inherits_live_execution_client():
    """oe-adapter-5.x: 继承 LiveExecutionClient,NT ExecutionEngine 能识别"""


@pytest.mark.skip(reason="not implemented; pending Step 5")
def test_exec_client_submit_order_with_event_writeback():
    """
    oe-adapter-5.x: 下单 + 完整事件回写

    Strategy.submit_order → _submit_order → Playwright 提交 → 收到 OE 响应 →
    generate_order_submitted / _filled 等推回 NT MessageBus → Strategy.on_order_* 触发。
    """


@pytest.mark.skip(reason="not implemented; pending Step 5")
def test_exec_client_cancel_order():
    """oe-adapter-5.x: 撤单 + generate_order_canceled 回写"""


@pytest.mark.skip(reason="not implemented; pending Step 5")
def test_exec_client_uses_execution_page():
    """oe-adapter-5.x: 通过 manager.get_page('execution') 提交,不调 start/close"""


@pytest.mark.skip(reason="not implemented; pending Step 5")
def test_exec_client_generates_order_status_reports():
    """
    oe-adapter-5.x: Reconciliation 接口

    NT ExecutionEngine 启动时调用,验证返回 venue 上当前所有挂单的
    NT 标准 OrderStatusReport。
    """
