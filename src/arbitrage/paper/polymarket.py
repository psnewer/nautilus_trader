"""Polymarket 私有执行协议的本地 paper 服务。

公开市场数据仍取真实 Polymarket；订单、成交、余额和仓位只保存在本地 SQLite。
该服务不验证签名和 API 凭证，它的边界是 localhost paper 运行模式。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import ClientSession
from aiohttp import ClientWebSocketResponse
from aiohttp import WSMsgType
from aiohttp import web


_LOG = logging.getLogger(__name__)
_END_CURSOR = "LTE="
_PUBLIC_GET_PREFIXES = (
    "/version",
    "/time",
    "/tick-size",
    "/neg-risk",
    "/fee-rate",
    "/markets-by-token/",
    "/clob-markets/",
    "/book",
)
_ORDER_AUDIT_COLUMNS = {
    "client_order_id": "TEXT",
    "pair_id": "TEXT",
    "opportunity_id": "TEXT",
    "leg_key": "TEXT",
    "intent": "TEXT",
    "origin": "TEXT",
    "instrument_id": "TEXT",
    "outcome": "TEXT",
    "selection_role": "TEXT",
    "strategy_id": "TEXT",
    "order_score": "TEXT",
    "ts_submitted_ns": "INTEGER",
}


@dataclass(frozen=True, slots=True)
class PaperServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    database_path: str = "var/polymarket-paper.sqlite3"
    starting_balance: float = 10_000.0
    reset: bool = False
    market_http_url: str = "https://clob.polymarket.com"
    market_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    market_proxy_url: str | None = None


class PaperStore:
    """小型 SQLite 权威账本；每次状态变更在同一事务内提交。"""

    def __init__(self, path: str, starting_balance: float, reset: bool = False) -> None:
        db_path = Path(path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if reset:
            # 建库前删三件套(主库 + WAL + SHM),保证账本只含当次运行、不跨运行累积。
            for suffix in ("", "-wal", "-shm"):
                removed = Path(f"{db_path}{suffix}")
                if removed.exists():
                    removed.unlink()
                    _LOG.info("PaperStore reset: removed %s", removed)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                market TEXT NOT NULL,
                asset_id TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                original_size REAL NOT NULL,
                size_matched REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                order_type TEXT NOT NULL,
                post_only INTEGER NOT NULL,
                maker_address TEXT NOT NULL,
                owner TEXT NOT NULL,
                expiration TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                market TEXT NOT NULL,
                asset_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                asset_id TEXT PRIMARY KEY,
                market TEXT NOT NULL,
                size REAL NOT NULL,
                total_cost REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """,
        )
        self._ensure_order_audit_columns()
        self._db.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES ('balance', ?)",
            (str(float(starting_balance)),),
        )
        self._db.commit()

    def _ensure_order_audit_columns(self) -> None:
        """为已有 paper 数据库原地补齐可空审计列。"""
        existing = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(orders)")
        }
        for name, sql_type in _ORDER_AUDIT_COLUMNS.items():
            if name not in existing:
                self._db.execute(f"ALTER TABLE orders ADD COLUMN {name} {sql_type}")

    def update_order_metadata(self, order_id: str, metadata: dict[str, Any]) -> bool:
        values = {
            name: metadata.get(name)
            for name in _ORDER_AUDIT_COLUMNS
            if name in metadata
        }
        if not values:
            return False
        assignments = ", ".join(f"{name} = ?" for name in values)
        cursor = self._db.execute(
            f"UPDATE orders SET {assignments} WHERE id = ?",
            (*values.values(), order_id),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        self._db.close()

    def insert_order(self, order: dict[str, Any]) -> None:
        self._db.execute(
            """INSERT INTO orders(
                id, market, asset_id, side, price, original_size, size_matched,
                status, order_type, post_only, maker_address, owner, expiration, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 'LIVE', ?, ?, ?, ?, ?, ?)""",
            (
                order["id"], order["market"], order["asset_id"], order["side"],
                order["price"], order["original_size"], order["order_type"],
                int(order["post_only"]), order["maker_address"], order["owner"],
                order.get("expiration"), order["created_at"],
            ),
        )
        self._db.commit()

    def order(self, order_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None

    def orders(self, *, open_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM orders"
        if open_only:
            query += " WHERE status = 'LIVE'"
        query += " ORDER BY created_at"
        return [dict(row) for row in self._db.execute(query)]

    def available_balance(self) -> float:
        """扣除未成交 BUY 限价名义额后的本地可用现金。"""
        balance = float(
            self._db.execute("SELECT value FROM metadata WHERE key = 'balance'").fetchone()[0],
        )
        reserved = self._db.execute(
            """SELECT COALESCE(SUM((original_size - size_matched) * price), 0)
            FROM orders WHERE status = 'LIVE' AND side = 'BUY'""",
        ).fetchone()[0]
        return balance - float(reserved)

    def available_position(self, asset_id: str) -> float:
        """扣除未成交 SELL 数量后的本地可用仓位。"""
        row = self._db.execute(
            "SELECT size FROM positions WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
        size = float(row[0]) if row else 0.0
        reserved = self._db.execute(
            """SELECT COALESCE(SUM(original_size - size_matched), 0)
            FROM orders WHERE status = 'LIVE' AND side = 'SELL' AND asset_id = ?""",
            (asset_id,),
        ).fetchone()[0]
        return size - float(reserved)

    def cancel(self, order_id: str) -> bool:
        cursor = self._db.execute(
            "UPDATE orders SET status = 'CANCELED' WHERE id = ? AND status = 'LIVE'",
            (order_id,),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def fill(self, order_id: str, price: float, trade: dict[str, Any]) -> dict[str, Any] | None:
        order = self.order(order_id)
        if order is None or order["status"] != "LIVE":
            return None
        qty = float(order["original_size"]) - float(order["size_matched"])
        if qty <= 0:
            return None

        position = self._db.execute(
            "SELECT size, total_cost FROM positions WHERE asset_id = ?",
            (order["asset_id"],),
        ).fetchone()
        old_size = float(position["size"]) if position else 0.0
        old_cost = float(position["total_cost"]) if position else 0.0
        balance = float(
            self._db.execute("SELECT value FROM metadata WHERE key = 'balance'").fetchone()[0],
        )
        if order["side"] == "BUY":
            new_size = old_size + qty
            new_cost = old_cost + qty * price
            balance -= qty * price
        else:
            sold = min(qty, old_size)
            avg_cost = old_cost / old_size if old_size > 0 else 0.0
            new_size = old_size - sold
            new_cost = max(0.0, old_cost - sold * avg_cost)
            balance += qty * price

        self._db.execute(
            "UPDATE orders SET status = 'MATCHED', size_matched = original_size WHERE id = ?",
            (order_id,),
        )
        self._db.execute(
            """INSERT INTO positions(asset_id, market, size, total_cost) VALUES (?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET market=excluded.market,
                size=excluded.size, total_cost=excluded.total_cost""",
            (order["asset_id"], order["market"], new_size, new_cost),
        )
        self._db.execute("UPDATE metadata SET value = ? WHERE key = 'balance'", (str(balance),))
        self._db.execute(
            """INSERT INTO trades(
                id, market, asset_id, order_id, created_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                trade["id"], order["market"], order["asset_id"], order_id,
                int(time.time()), json.dumps(trade, separators=(",", ":")),
            ),
        )
        self._db.commit()
        return self.order(order_id)

    def trades(self) -> list[dict[str, Any]]:
        return [
            json.loads(row[0])
            for row in self._db.execute("SELECT payload FROM trades ORDER BY created_at")
        ]

    def positions(self) -> list[dict[str, Any]]:
        result = []
        for row in self._db.execute("SELECT * FROM positions WHERE size > 0.000001"):
            size = float(row["size"])
            cost = float(row["total_cost"])
            result.append({
                "conditionId": row["market"],
                "asset": row["asset_id"],
                "size": size,
                "avgPrice": cost / size if size else 0.0,
                "redeemable": False,
                "negRisk": False,
            })
        return result

    def balance_units(self) -> int:
        row = self._db.execute("SELECT value FROM metadata WHERE key = 'balance'").fetchone()
        value = float(row[0])
        return round(value * 1_000_000)


class PolymarketPaperExchange:
    """本地订单状态机；真实 PM Market WS 逐帧驱动模拟撮合。"""

    def __init__(self, config: PaperServerConfig, store: PaperStore | None = None) -> None:
        self.config = config
        self.store = store or PaperStore(
            config.database_path,
            config.starting_balance,
            reset=config.reset,
        )
        self._session: ClientSession | None = None
        self._websockets: set[web.WebSocketResponse] = set()
        self._market_ws: ClientWebSocketResponse | None = None
        self._market_ws_task: asyncio.Task | None = None
        self._market_subscriptions: set[str] = set()
        self._market_books: dict[str, tuple[float | None, float | None]] = {}
        self._market_levels: dict[str, dict[str, dict[float, float]]] = {}
        self._market_ready: dict[str, asyncio.Event] = {}
        self._market_pending_placements: dict[str, int] = {}
        self._market_subscription_changed = asyncio.Event()
        self._market_send_lock = asyncio.Lock()
        self._placement_events: dict[str, dict[str, Any]] = {}
        self._pending_trade_events: dict[str, dict[str, Any]] = {}
        self._placement_sent: set[str] = set()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._place_lock = asyncio.Lock()

    async def start(self) -> None:
        self._session = ClientSession()
        self._market_subscriptions.update({
            str(order["asset_id"])
            for order in self.store.orders(open_only=True)
        })
        if self._market_subscriptions:
            self._market_subscription_changed.set()
        self._market_ws_task = asyncio.create_task(
            self._run_market_ws(),
            name="pm-paper-market-ws",
        )

    async def stop(self) -> None:
        if self._market_ws_task:
            self._market_ws_task.cancel()
            await asyncio.gather(self._market_ws_task, return_exceptions=True)
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        if self._market_ws is not None:
            await self._market_ws.close()
        for ws in tuple(self._websockets):
            await ws.close()
        if self._session:
            await self._session.close()
        self.store.close()

    async def public_get(self, path_qs: str) -> tuple[int, bytes, str]:
        if not path_qs.startswith(_PUBLIC_GET_PREFIXES):
            raise web.HTTPNotFound()
        assert self._session is not None
        url = self.config.market_http_url.rstrip("/") + path_qs
        async with self._session.get(url, proxy=self.config.market_proxy_url) as response:
            content_type = response.headers.get("Content-Type", "application/json")
            return response.status, await response.read(), content_type

    async def resolve_market(self, token_id: str) -> str:
        status, body, _ = await self.public_get(f"/markets-by-token/{token_id}")
        if status >= 400:
            raise web.HTTPBadGateway(text=f"PM public token lookup failed: HTTP {status}")
        payload = json.loads(body)
        condition_id = payload.get("condition_id")
        if not condition_id:
            raise web.HTTPBadGateway(text="PM public token lookup missing condition_id")
        return str(condition_id)

    async def best_prices(self, token_id: str) -> tuple[float | None, float | None]:
        return self._market_books.get(token_id, (None, None))

    async def ensure_market_subscription(self, token_id: str) -> None:
        """下单前确保 Market WS 已收到该 token 的初始盘口。"""
        ready = self._market_ready.setdefault(token_id, asyncio.Event())
        if token_id in self._market_subscriptions and token_id in self._market_books:
            return
        is_new = token_id not in self._market_subscriptions
        if is_new:
            # 同一 binary market 的消息可能顺带携带对手 token, 不能把这种偶得盘口
            # 当成该 token 已订阅; 重新订阅并等待自己的初始 book。
            self._market_books.pop(token_id, None)
            self._market_levels.pop(token_id, None)
            ready.clear()
        self._market_subscriptions.add(token_id)
        self._market_subscription_changed.set()
        if is_new and self._market_ws is not None and not self._market_ws.closed:
            async with self._market_send_lock:
                await self._market_ws.send_json({
                    "assets_ids": [token_id],
                    "operation": "subscribe",
                })
            _LOG.info("PAPER_MARKET_WS subscribed token_id=%s", token_id)
        try:
            await asyncio.wait_for(ready.wait(), timeout=15.0)
        except TimeoutError as exc:
            raise web.HTTPBadGateway(text="PM market WS initial book timeout") from exc

    def _retain_market_placement(self, token_id: str) -> None:
        self._market_pending_placements[token_id] = (
            self._market_pending_placements.get(token_id, 0) + 1
        )

    async def _release_market_placement(self, token_id: str) -> None:
        pending = self._market_pending_placements[token_id] - 1
        if pending > 0:
            self._market_pending_placements[token_id] = pending
        else:
            self._market_pending_placements.pop(token_id, None)
        await self._release_market_subscription_if_unused(token_id)

    async def _run_market_ws(self) -> None:
        assert self._session is not None
        while True:
            if not self._market_subscriptions:
                self._market_subscription_changed.clear()
                await self._market_subscription_changed.wait()
            try:
                async with self._session.ws_connect(
                    self.config.market_ws_url,
                    proxy=self.config.market_proxy_url,
                    heartbeat=10,
                ) as ws:
                    self._market_ws = ws
                    await ws.send_json({
                        "type": "market",
                        "assets_ids": sorted(self._market_subscriptions),
                    })
                    _LOG.info(
                        "PAPER_MARKET_WS connected subscriptions=%d",
                        len(self._market_subscriptions),
                    )
                    async for message in ws:
                        if message.type == WSMsgType.TEXT:
                            if message.data.strip().upper() == "PING":
                                await ws.send_str("PONG")
                            else:
                                await self._handle_market_ws_payload(message.data)
                        elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                            break
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("PM paper Market WS disconnected")
            finally:
                self._market_ws = None
                self._market_books.clear()
                self._market_levels.clear()
                for ready in self._market_ready.values():
                    ready.clear()
            await asyncio.sleep(1.0)

    async def _handle_market_ws_payload(self, raw: str) -> None:
        if raw.strip().upper() in {"PING", "PONG"}:
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            _LOG.warning("Ignoring invalid PM Market WS payload: %s", raw[:200])
            return
        messages = payload if isinstance(payload, list) else [payload]
        for message in messages:
            event_type = message.get("event_type")
            if event_type == "book":
                token_id = str(message.get("asset_id") or "")
                await self._replace_market_levels(
                    token_id,
                    bids=message.get("bids") or [],
                    asks=message.get("asks") or [],
                )
            elif event_type == "price_change":
                for change in message.get("price_changes") or []:
                    token_id = str(change.get("asset_id") or "")
                    levels = self._market_levels.get(token_id)
                    if levels is not None:
                        await self._apply_market_level_change(token_id, change)
                        continue
                    bid = change.get("best_bid")
                    ask = change.get("best_ask")
                    if bid is not None and ask is not None:
                        await self._on_market_prices(token_id, float(bid), float(ask))

    async def _replace_market_levels(
        self,
        token_id: str,
        *,
        bids: list[dict[str, Any]],
        asks: list[dict[str, Any]],
    ) -> None:
        levels = {
            "BUY": {
                float(level["price"]): float(level["size"])
                for level in bids
                if float(level["size"]) > 0
            },
            "SELL": {
                float(level["price"]): float(level["size"])
                for level in asks
                if float(level["size"]) > 0
            },
        }
        bid = max(levels["BUY"], default=None)
        ask = min(levels["SELL"], default=None)
        previous = self._market_levels.get(token_id)
        self._market_levels[token_id] = levels
        if not await self._on_market_prices(token_id, bid, ask):
            if previous is None:
                self._market_levels.pop(token_id, None)
            else:
                self._market_levels[token_id] = previous

    async def _apply_market_level_change(
        self,
        token_id: str,
        change: dict[str, Any],
    ) -> None:
        side = str(change.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            return
        price = float(change["price"])
        size = float(change["size"])
        current = self._market_levels[token_id]
        changed_side = dict(current[side])
        if size > 0:
            changed_side[price] = size
        else:
            changed_side.pop(price, None)
        bid_levels = changed_side if side == "BUY" else current["BUY"]
        ask_levels = changed_side if side == "SELL" else current["SELL"]
        bid = max(bid_levels, default=None)
        ask = min(ask_levels, default=None)
        changed = dict(current)
        changed[side] = changed_side
        self._market_levels[token_id] = changed
        if not await self._on_market_prices(token_id, bid, ask):
            self._market_levels[token_id] = current

    async def _on_market_prices(
        self,
        token_id: str,
        bid: float | None,
        ask: float | None,
    ) -> bool:
        if not token_id:
            return False
        if (
            bid is not None
            and ask is not None
            and (bid <= 0 or ask <= 0 or bid >= ask)
        ):
            _LOG.warning(
                "Dropping non-positive-spread paper market frame: token_id=%s bid=%s ask=%s",
                token_id,
                bid,
                ask,
            )
            return False
        self._market_books[token_id] = (bid, ask)
        self._market_ready.setdefault(token_id, asyncio.Event()).set()
        await self._match_resting_orders(token_id, bid, ask)
        return True

    async def place(self, payload: dict[str, Any]) -> dict[str, Any]:
        signed = payload.get("order") or {}
        token_id = str(signed.get("tokenId") or "")
        side = str(signed.get("side") or "").upper()
        if not token_id or side not in {"BUY", "SELL"}:
            return {"success": False, "errorMsg": "invalid paper order payload"}
        maker_amount = int(signed.get("makerAmount") or 0) / 1_000_000
        taker_amount = int(signed.get("takerAmount") or 0) / 1_000_000
        if maker_amount <= 0 or taker_amount <= 0:
            return {"success": False, "errorMsg": "invalid paper order amount"}
        price = maker_amount / taker_amount if side == "BUY" else taker_amount / maker_amount
        size = taker_amount if side == "BUY" else maker_amount
        order_id = "0x" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        ).hexdigest()
        if self.store.order(order_id) is not None:
            return {"success": True, "orderID": order_id, "status": "live"}
        self._retain_market_placement(token_id)
        try:
            market = await self.resolve_market(token_id)
            await self.ensure_market_subscription(token_id)
            now = int(time.time())
            order = {
                "id": order_id,
                "market": market,
                "asset_id": token_id,
                "side": side,
                "price": price,
                "original_size": size,
                "order_type": str(payload.get("orderType") or "GTC"),
                "post_only": bool(payload.get("postOnly")),
                "maker_address": str(signed.get("maker") or "paper"),
                "owner": str(payload.get("owner") or "paper"),
                "expiration": str(signed.get("expiration") or "0"),
                "created_at": now,
            }
            if order["post_only"]:
                bid, ask = await self.best_prices(token_id)
                crosses = (
                    side == "BUY" and ask is not None and ask <= price
                ) or (
                    side == "SELL" and bid is not None and bid >= price
                )
                if crosses:
                    return {"success": False, "errorMsg": "post-only order would cross"}

            # 市场查询含 await；余额/仓位检查必须在其后串行完成，否则并发请求可能同时
            # 看到相同可用额并产生超额预占。锁内没有网络 I/O，临界区只覆盖账本操作。
            async with self._place_lock:
                if self.store.order(order_id) is not None:
                    return {"success": True, "orderID": order_id, "status": "live"}
                if side == "BUY" and size * price > self.store.available_balance() + 1e-9:
                    return {"success": False, "errorMsg": "insufficient paper balance"}
                if side == "SELL" and size > self.store.available_position(token_id) + 1e-9:
                    return {"success": False, "errorMsg": "insufficient paper position"}
                self.store.insert_order(order)
                stored_order = self.store.order(order_id)
                assert stored_order is not None
                self._placement_events[order_id] = self._order_event(stored_order, "PLACEMENT")
                bid, ask = await self.best_prices(token_id)
                trade = self._match_order(stored_order, bid, ask, immediate=True)
                if trade is not None:
                    self._pending_trade_events[order_id] = trade
        finally:
            await self._release_market_placement(token_id)
        task = asyncio.create_task(self._after_place(order_id))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return {"success": True, "orderID": order_id, "status": "live"}

    async def _after_place(self, order_id: str) -> None:
        await asyncio.sleep(0.01)  # 让 HTTP 回执先建立 venue_order_id 映射。
        order = self.store.order(order_id)
        if order is None:
            return
        placement = self._placement_events.pop(order_id, None)
        if placement is not None:
            await self.broadcast(placement)
        self._placement_sent.add(order_id)
        trade = self._pending_trade_events.pop(order_id, None)
        if trade is not None:
            await self.broadcast(trade)
            self._placement_sent.discard(order_id)
            await self._release_market_subscription_if_unused(str(order["asset_id"]))
            return
        if order["order_type"] in {"FAK", "FOK", "IOC"}:
            await self.cancel(order_id)

    def _match_order(
        self,
        order: dict[str, Any],
        bid: float | None,
        ask: float | None,
        *,
        immediate: bool,
    ) -> dict[str, Any] | None:
        if order["status"] != "LIVE":
            return None
        if order["side"] == "BUY":
            marketable = ask is not None and ask <= float(order["price"])
            taker_price = ask
        else:
            marketable = bid is not None and bid >= float(order["price"])
            taker_price = bid
        if not marketable or taker_price is None:
            return None
        match_price = float(taker_price) if immediate else float(order["price"])
        trade = self._trade_event(order, float(match_price), immediate=immediate)
        if self.store.fill(order["id"], float(match_price), trade) is None:
            return None
        _LOG.info(
            "PAPER_MATCH order_id=%s token_id=%s liquidity=%s limit=%s quote=%s fill=%s",
            order["id"],
            order["asset_id"],
            "taker" if immediate else "maker",
            order["price"],
            taker_price,
            match_price,
        )
        return trade

    async def _match_resting_orders(
        self,
        token_id: str,
        bid: float | None,
        ask: float | None,
    ) -> None:
        for order in self.store.orders(open_only=True):
            if str(order["asset_id"]) != token_id:
                continue
            if order["order_type"] in {"FAK", "FOK", "IOC"}:
                continue
            trade = self._match_order(order, bid, ask, immediate=False)
            if trade is None:
                continue
            order_id = str(order["id"])
            if order_id in self._placement_sent:
                await self.broadcast(trade)
                self._placement_sent.discard(order_id)
            else:
                self._pending_trade_events[order_id] = trade
        await self._release_market_subscription_if_unused(token_id)

    async def cancel(self, order_id: str) -> bool:
        if not self.store.cancel(order_id):
            return False
        order = self.store.order(order_id)
        assert order is not None
        await self.broadcast(self._order_event(order, "CANCELLATION"))
        self._placement_events.pop(order_id, None)
        self._pending_trade_events.pop(order_id, None)
        self._placement_sent.discard(order_id)
        await self._release_market_subscription_if_unused(str(order["asset_id"]))
        return True

    async def _release_market_subscription_if_unused(self, token_id: str) -> None:
        if self._market_pending_placements.get(token_id, 0) > 0:
            return
        if any(
            str(order["asset_id"]) == token_id
            for order in self.store.orders(open_only=True)
        ):
            return
        if token_id not in self._market_subscriptions:
            return
        self._market_subscriptions.discard(token_id)
        self._market_books.pop(token_id, None)
        self._market_levels.pop(token_id, None)
        self._market_ready.pop(token_id, None)
        if self._market_ws is not None and not self._market_ws.closed:
            async with self._market_send_lock:
                await self._market_ws.send_json({
                    "assets_ids": [token_id],
                    "operation": "unsubscribe",
                })
        _LOG.info("PAPER_MARKET_WS unsubscribed token_id=%s", token_id)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        stale = []
        for ws in tuple(self._websockets):
            try:
                await ws.send_json(payload)
            except (ConnectionError, RuntimeError):
                stale.append(ws)
        for ws in stale:
            self._websockets.discard(ws)

    @staticmethod
    def order_report(order: dict[str, Any]) -> dict[str, Any]:
        return {
            "associate_trades": [],
            "id": order["id"],
            "status": order["status"],
            "market": order["market"],
            "original_size": str(order["original_size"]),
            "outcome": "",
            "maker_address": order["maker_address"],
            "owner": order["owner"],
            "price": str(order["price"]),
            "side": order["side"],
            "size_matched": str(order["size_matched"]),
            "asset_id": order["asset_id"],
            "expiration": order["expiration"],
            "order_type": order["order_type"],
            "created_at": int(order["created_at"]),
        }

    def _order_event(self, order: dict[str, Any], event_type: str) -> dict[str, Any]:
        report = self.order_report(order)
        report.update({
            "event_type": "order",
            "created_at": str(order["created_at"] * 1000),
            "order_owner": order["owner"],
            "timestamp": str(round(time.time() * 1000)),
            "type": event_type,
        })
        return report

    @staticmethod
    def _trade_event(order: dict[str, Any], price: float, *, immediate: bool) -> dict[str, Any]:
        now = int(time.time())
        digest = hashlib.sha256(f"{order['id']}:{now}:{price}".encode()).hexdigest()
        trade_id = "paper-" + digest[:24]
        if immediate:
            trader_side = "TAKER"
            trade_side = order["side"]
            maker_orders = []
            taker_order_id = order["id"]
        else:
            trader_side = "MAKER"
            trade_side = "SELL" if order["side"] == "BUY" else "BUY"
            maker_orders = [{
                "asset_id": order["asset_id"],
                "fee_rate_bps": "0",
                "maker_address": order["maker_address"],
                "matched_amount": str(order["original_size"]),
                "order_id": order["id"],
                "outcome": "",
                "owner": order["owner"],
                "price": str(price),
            }]
            taker_order_id = "paper-taker-" + trade_id
        return {
            "event_type": "trade",
            "asset_id": order["asset_id"],
            "bucket_index": 0,
            "fee_rate_bps": "0",
            "id": trade_id,
            "last_update": str(now),
            "maker_address": order["maker_address"],
            "maker_orders": maker_orders,
            "market": order["market"],
            "match_time": str(now),
            "outcome": "",
            "owner": order["owner"],
            "price": str(price),
            "side": trade_side,
            "size": str(order["original_size"]),
            "status": "CONFIRMED",
            "transaction_hash": "",
            "taker_order_id": taker_order_id,
            "timestamp": str(now * 1000),
            "trade_owner": order["owner"],
            "trader_side": trader_side,
            "type": "TRADE",
        }


def create_app(exchange: PolymarketPaperExchange) -> web.Application:
    app = web.Application()

    async def on_startup(_app: web.Application) -> None:
        await exchange.start()

    async def on_cleanup(_app: web.Application) -> None:
        await exchange.stop()

    async def public_proxy(request: web.Request) -> web.Response:
        status, body, content_type = await exchange.public_get(request.path_qs)
        return web.Response(status=status, body=body, headers={"Content-Type": content_type})

    async def post_order(request: web.Request) -> web.Response:
        return web.json_response(await exchange.place(await request.json()))

    async def post_orders(request: web.Request) -> web.Response:
        return web.json_response([await exchange.place(item) for item in await request.json()])

    async def get_order(request: web.Request) -> web.Response:
        order = exchange.store.order(request.match_info["order_id"])
        if order is None:
            raise web.HTTPNotFound()
        return web.json_response(exchange.order_report(order))

    async def get_orders(request: web.Request) -> web.Response:
        rows = exchange.store.orders(open_only=True)
        market = request.query.get("market")
        asset_id = request.query.get("asset_id")
        if market:
            rows = [row for row in rows if row["market"] == market]
        if asset_id:
            rows = [row for row in rows if row["asset_id"] == asset_id]
        return web.json_response({
            "data": [exchange.order_report(row) for row in rows],
            "next_cursor": _END_CURSOR,
        })

    async def get_trades(request: web.Request) -> web.Response:
        rows = exchange.store.trades()
        market = request.query.get("market")
        asset_id = request.query.get("asset_id")
        trade_id = request.query.get("id")
        after = int(request.query["after"]) if request.query.get("after") else None
        before = int(request.query["before"]) if request.query.get("before") else None
        if market:
            rows = [row for row in rows if row["market"] == market]
        if asset_id:
            rows = [row for row in rows if row["asset_id"] == asset_id]
        if trade_id:
            rows = [row for row in rows if row["id"] == trade_id]
        if after is not None:
            rows = [row for row in rows if int(row["match_time"]) >= after]
        if before is not None:
            rows = [row for row in rows if int(row["match_time"]) <= before]
        return web.json_response({"data": rows, "next_cursor": _END_CURSOR})

    async def cancel_order(request: web.Request) -> web.Response:
        order_id = str((await request.json()).get("orderID") or "")
        canceled = await exchange.cancel(order_id)
        return web.json_response({
            "canceled": [order_id] if canceled else [],
            "not_canceled": {} if canceled else {order_id: "not open"},
        })

    async def cancel_orders(request: web.Request) -> web.Response:
        order_ids = [str(value) for value in await request.json()]
        canceled = [order_id for order_id in order_ids if await exchange.cancel(order_id)]
        return web.json_response({
            "canceled": canceled,
            "not_canceled": {
                value: "not open" for value in order_ids if value not in canceled
            },
        })

    async def cancel_all(_request: web.Request) -> web.Response:
        ids = [row["id"] for row in exchange.store.orders(open_only=True)]
        canceled = [order_id for order_id in ids if await exchange.cancel(order_id)]
        return web.json_response({"canceled": canceled, "not_canceled": {}})

    async def cancel_market_orders(request: web.Request) -> web.Response:
        payload = await request.json()
        market = payload.get("market")
        asset_id = payload.get("asset_id")
        rows = exchange.store.orders(open_only=True)
        if market:
            rows = [row for row in rows if row["market"] == market]
        if asset_id:
            rows = [row for row in rows if row["asset_id"] == asset_id]
        ids = [row["id"] for row in rows]
        canceled = [order_id for order_id in ids if await exchange.cancel(order_id)]
        return web.json_response({"canceled": canceled, "not_canceled": {}})

    async def balance(_request: web.Request) -> web.Response:
        return web.json_response({"balance": str(exchange.store.balance_units()), "allowances": {}})

    async def positions(request: web.Request) -> web.Response:
        offset = int(request.query.get("offset", 0))
        limit = int(request.query.get("limit", 100))
        return web.json_response(exchange.store.positions()[offset:offset + limit])

    async def closed_positions(_request: web.Request) -> web.Response:
        return web.json_response([])

    async def order_metadata(request: web.Request) -> web.Response:
        payload = await request.json()
        order_id = str(payload.pop("order_id", "") or "")
        updated = bool(order_id) and exchange.store.update_order_metadata(order_id, payload)
        return web.json_response({"success": updated}, status=200 if updated else 404)

    async def user_ws(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        exchange._websockets.add(ws)
        try:
            async for _message in ws:
                pass  # 订阅消息只用于保持与真实 USER WS 客户端的协议兼容。
        finally:
            exchange._websockets.discard(ws)
        return ws

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    for path in _PUBLIC_GET_PREFIXES:
        route = path + "{tail:.*}" if path.endswith("/") else path
        app.router.add_get(route, public_proxy)
    app.router.add_post("/order", post_order)
    app.router.add_post("/orders", post_orders)
    app.router.add_get("/data/order/{order_id}", get_order)
    app.router.add_get("/data/orders", get_orders)
    app.router.add_get("/data/trades", get_trades)
    app.router.add_delete("/order", cancel_order)
    app.router.add_delete("/orders", cancel_orders)
    app.router.add_delete("/cancel-all", cancel_all)
    app.router.add_delete("/cancel-market-orders", cancel_market_orders)
    app.router.add_get("/balance-allowance", balance)
    app.router.add_get("/positions", positions)
    app.router.add_get("/closed-positions", closed_positions)
    app.router.add_post("/paper/order-metadata", order_metadata)
    app.router.add_get("/ws/user", user_ws)
    return app


def run_server(config: PaperServerConfig) -> None:
    exchange = PolymarketPaperExchange(config)
    web.run_app(create_app(exchange), host=config.host, port=config.port)
