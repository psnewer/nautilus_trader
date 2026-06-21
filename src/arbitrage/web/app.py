"""FastAPI app builder —— WebGatewayActor 的 HTTP/WS 路由(Step 7 只读监控 MVP)。

路由只读 `actor` 持有的 `portfolio` pull 方法、`cache` 账户快照、以及 actor 缓存的 matched_pairs;
WS 经 actor 注册的 `asyncio.Queue` 流式推。**纯只读,不发命令、不写 cache。**
设计:`docs/arbitrage/architectures/web/architecture.md §3.2`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi import WebSocket
from fastapi import WebSocketDisconnect

if TYPE_CHECKING:
    from src.arbitrage.web.actor import WebGatewayActor


def build_app(actor: "WebGatewayActor") -> FastAPI:
    """构造 FastAPI app。`actor` 只需暴露:`_portfolio` / `accounts_snapshot()` /
    `matched_pairs()` / `register_ws()` / `unregister_ws(q)`(便于测试用 stub 替身)。"""
    app = FastAPI(title="arb-web-gateway", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/accounts")
    async def accounts() -> list:
        return actor.accounts_snapshot()

    @app.get("/matched_pairs")
    async def matched_pairs() -> list:
        return actor.matched_pairs()

    # 注意:此路由必须先于 `/positions/{pair_id}` 注册,否则被路径参数吞掉。
    @app.get("/positions/global_min_rebate_sum")
    async def global_min_rebate_sum() -> dict:
        return {"global_min_rebate_sum": actor._portfolio.global_min_rebate_sum()}

    @app.get("/positions/{pair_id}")
    async def positions(pair_id: str) -> dict:
        pf = actor._portfolio
        return {
            "pair_id": pair_id,
            "way_rebate": pf.way_rebate(pair_id),
            "way_rebates_by_venue": pf.way_rebates_by_venue(pair_id),
            "outcome_exposures": {
                outcome: {"net_profit": exp.net_profit, "liability": exp.liability}
                for outcome, exp in pf.outcome_exposures(pair_id).items()
            },
        }

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = actor.register_ws()
        try:
            while True:
                msg = await queue.get()
                if msg is None:  # on_stop 投的毒丸 → 优雅退出
                    break
                await websocket.send_json(msg)
        except WebSocketDisconnect:
            pass
        finally:
            actor.unregister_ws(queue)

    return app
