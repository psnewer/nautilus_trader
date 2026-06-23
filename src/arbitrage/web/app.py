"""FastAPI app builder —— WebGatewayActor 的控制台路由(Step 7 §8)。

控制面:TradingState 启停 + 配置编辑(读经 risk_engine 引用,写经 MessageBus 命令)+ `/ws` 推
TradingState 变更。纯只读监控 endpoint(余额/matched_pairs/way_rebate)已移除(2026-06-21)。
设计:`docs/arbitrage/architectures/web/architecture.md §8.4`。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Body
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import HTMLResponse

if TYPE_CHECKING:
    from src.arbitrage.web.actor import WebGatewayActor

_CONSOLE_HTML = (Path(__file__).parent / "static" / "console.html").read_text(encoding="utf-8")


def build_app(actor: "WebGatewayActor") -> FastAPI:
    """构造 FastAPI app。`actor` 需暴露:`trading_state()` / `set_trading_state(s)` /
    `config_snapshot()` / `update_config_section(sec, fields)` / `register_ws()` / `unregister_ws(q)`
    (便于测试用 stub 替身)。"""
    app = FastAPI(title="arb-web-gateway", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _CONSOLE_HTML

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    # ── 只读快照:余额 + 赔率(周期 GET)──────────────────────────────
    @app.get("/accounts")
    async def accounts() -> list:
        return actor.accounts_snapshot()

    @app.get("/odds")
    async def odds() -> list:
        return actor.odds_snapshot()

    @app.get("/matched_pairs")
    async def matched_pairs() -> list:
        return actor.matched_pairs()

    @app.get("/instruments")
    async def instruments() -> list:
        return actor.instruments_snapshot()

    # ── 控制台:TradingState 启停(§8.1)──────────────────────────────
    @app.get("/control/trading_state")
    async def get_trading_state() -> dict:
        return {"trading_state": actor.trading_state()}

    @app.post("/control/trading_state")
    async def post_trading_state(body: dict = Body(...)) -> dict:
        state = str(body.get("state", "")).upper()
        if state not in ("ACTIVE", "HALTED"):
            raise HTTPException(status_code=400, detail="state must be ACTIVE or HALTED")
        actor.set_trading_state(state)  # publish 命令(方案乙);risk engine 异步 apply
        return {"status": "ok", "requested": state}

    # ── 控制台:配置编辑(§8.2)────────────────────────────────────────
    @app.get("/config")
    async def get_config() -> dict:
        return actor.config_snapshot()

    @app.put("/config/{section}")
    async def put_config(section: str, body: dict = Body(...)) -> dict:
        try:
            return actor.update_config_section(section, body)
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=str(e))

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
