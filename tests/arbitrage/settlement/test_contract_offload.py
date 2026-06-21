"""contract.py 链上调用不阻塞 event loop(settlement-8.8)。

`RelayClient.execute` / `resp.wait()` 是同步阻塞调用;`merge_positions`/`redeem_positions`
经 `loop.run_in_executor` 丢线程池。本测试用阻塞 fake `_execute_with_proxy` + 并发心跳协程
证明:链上 tx 跑的那 ~0.3s 里,event loop 仍能正常调度其它任务(心跳持续推进)。
若退回直接同步调用,心跳会被饿死(计数停在 0~1)。
"""

import asyncio
import logging
import time

from nautilus_trader.adapters.polymarket.contract import PolymarketContractService


_COND = "0x" + "11" * 32       # 合法 bytes32 condition id,供 encode 用
_BLOCK_SECS = 0.3


class _FakeResp:
    def wait(self):
        time.sleep(_BLOCK_SECS)        # 模拟等链上确认(同步阻塞)
        return type("Awaited", (), {"transactionHash": "0xabc"})()


def _service() -> PolymarketContractService:
    svc = PolymarketContractService.__new__(PolymarketContractService)
    svc._initialized = True
    svc._client = object()
    svc._log = logging.getLogger("test_contract_offload")

    def _blocking_execute(transactions, metadata=""):
        time.sleep(_BLOCK_SECS)        # 模拟提交 tx(同步阻塞)
        return _FakeResp()

    svc._execute_with_proxy = _blocking_execute
    return svc


async def _heartbeat(stop: asyncio.Event) -> int:
    ticks = 0
    while not stop.is_set():
        await asyncio.sleep(0.01)
        ticks += 1
    return ticks


async def _drive(coro):
    """跑 coro,同时数心跳;返回 (result, ticks)。"""
    stop = asyncio.Event()
    hb = asyncio.create_task(_heartbeat(stop))
    result = await coro
    stop.set()
    ticks = await hb
    return result, ticks


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_merge_does_not_block_event_loop():
    svc = _service()
    tx, ticks = _run(_drive(svc.merge_positions(condition_id=_COND, amount=50.0)))
    assert tx.success and tx.tx_hash == "0xabc"
    # 阻塞 ~0.6s(execute + wait),心跳每 10ms 一次 → loop 没冻则远超 10 次。
    assert ticks > 10, f"event loop starved during merge: only {ticks} heartbeats"


def test_redeem_does_not_block_event_loop():
    svc = _service()
    tx, ticks = _run(_drive(svc.redeem_positions(condition_id=_COND, neg_risk=False)))
    assert tx.success and tx.tx_hash == "0xabc"
    assert ticks > 10, f"event loop starved during redeem: only {ticks} heartbeats"
