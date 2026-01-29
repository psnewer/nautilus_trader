"""
Polymarket 赔率客户端

使用 Polymarket Gamma API 和 WebSocket 获取实时赔率数据。
"""

import asyncio
import logging
from typing import Any, Callable
import httpx
import json

from .config import OddsSubscriptionConfig


class PolymarketOddsClient:
    """
    Polymarket 赔率客户端

    功能：
    1. 根据 event_id 查询所有 tokens (home/draw/away)
    2. 订阅 token 的实时价格变化
    3. 解析并返回赔率数据
    """

    def __init__(
        self,
        config: OddsSubscriptionConfig,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self._log = logger or logging.getLogger(self.__class__.__name__)

        # WebSocket 连接
        self._ws_client: Any = None
        self._ws_task: asyncio.Task | None = None

        # 订阅管理
        self._subscribed_tokens: dict[str, dict] = {}  # token_id -> event_info
        self._latest_odds: dict[str, dict] = {}  # token_id -> odds_data

        # 回调函数
        self._price_update_callback: Callable[[dict], None] | None = None

    # =========================================================================
    # API 查询
    # =========================================================================

    async def get_event_tokens(self, event_id: str) -> list[dict[str, Any]]:
        """
        获取 event 的所有 tokens

        Args:
            event_id: Polymarket event ID

        Returns:
            tokens 列表，每个包含:
            - token_id: str
            - outcome: str (如 "Yes", "No", team name等)
            - market_type: str (home/draw/away，需要推断)
        """
        self._log.info(f"Fetching tokens for event {event_id}")

        # 使用 Gamma API 查询 event
        url = f"https://gamma-api.polymarket.com/events/{event_id}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                event_data = resp.json()

                # 提取 tokens
                tokens = []
                markets = event_data.get("markets", [])

                if not markets:
                    self._log.warning(f"No markets found for event {event_id}")
                    return tokens

                for market in markets:
                    # Polymarket sports events 通常有一个 main market
                    # 包含多个 outcomes (tokens)
                    outcomes = market.get("outcomes", [])
                    clob_token_ids_raw = market.get("clobTokenIds", "[]")

                    # clobTokenIds 是一个 JSON 字符串，需要解析
                    try:
                        if isinstance(clob_token_ids_raw, str):
                            clob_token_ids = json.loads(clob_token_ids_raw)
                        else:
                            clob_token_ids = clob_token_ids_raw
                    except json.JSONDecodeError:
                        self._log.warning(f"Failed to parse clobTokenIds: {clob_token_ids_raw}")
                        clob_token_ids = []

                    for i, outcome in enumerate(outcomes):
                        if i < len(clob_token_ids):
                            token_id = clob_token_ids[i]
                            market_type = self._infer_market_type(outcome, i, len(outcomes))

                            tokens.append({
                                "token_id": token_id,
                                "outcome": outcome,
                                "market_type": market_type,
                                "event_id": event_id,
                            })

                self._log.info(f"Found {len(tokens)} tokens for event {event_id}")
                return tokens

            except Exception as e:
                self._log.error(f"Failed to fetch tokens for event {event_id}: {e}")
                return []

    def _infer_market_type(self, outcome: str, index: int, total_outcomes: int) -> str:
        """
        推断 market type (home/draw/away)

        规则：
        - 2 outcomes: home/away
        - 3 outcomes: home/draw/away
        """
        if total_outcomes == 2:
            return "home" if index == 0 else "away"
        elif total_outcomes == 3:
            if index == 0:
                return "home"
            elif index == 1:
                return "draw"
            else:
                return "away"
        else:
            return f"outcome_{index}"

    # =========================================================================
    # WebSocket 订阅
    # =========================================================================

    async def subscribe_event(self, event_id: str) -> None:
        """
        订阅 event 的所有 tokens

        Args:
            event_id: Polymarket event ID
        """
        # 1. 获取 tokens
        tokens = await self.get_event_tokens(event_id)

        if not tokens:
            self._log.warning(f"No tokens to subscribe for event {event_id}")
            return

        # 2. 订阅每个 token
        for token_info in tokens:
            token_id = token_info["token_id"]
            self._subscribed_tokens[token_id] = token_info

        # 3. 启动 WebSocket 连接（如果还未连接）
        if not self._ws_task or self._ws_task.done():
            self._ws_task = asyncio.create_task(self._run_websocket())

        self._log.info(f"Subscribed to {len(tokens)} tokens for event {event_id}")

    async def _run_websocket(self) -> None:
        """
        运行 WebSocket 连接

        注意：这是一个简化实现，实际应使用 websockets 库
        """
        self._log.info("Starting WebSocket connection (placeholder)")

        # TODO: 实际实现 WebSocket 连接和订阅
        # 参考 nautilus_trader/adapters/polymarket/websocket/client.py

        # 临时方案：使用 HTTP 轮询模拟
        await self._polling_loop()

    async def _polling_loop(self) -> None:
        """
        轮询模拟 WebSocket（临时方案）

        实际生产环境应替换为真实 WebSocket
        """
        self._log.info("Starting polling loop (temporary implementation)")

        while True:
            try:
                # 轮询每个订阅的 token
                for token_id, token_info in list(self._subscribed_tokens.items()):
                    await self._fetch_token_price(token_id, token_info)

                # 每 5 秒轮询一次
                await asyncio.sleep(5)

            except asyncio.CancelledError:
                self._log.info("Polling loop cancelled")
                break
            except Exception as e:
                self._log.error(f"Error in polling loop: {e}")
                await asyncio.sleep(5)

    async def _fetch_token_price(self, token_id: str, token_info: dict) -> None:
        """
        获取 token 的当前价格（临时方案）

        TODO: 替换为 WebSocket 实时数据
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # 使用 midpoint 端点获取中间价
                midpoint_url = f"https://clob.polymarket.com/midpoint"
                resp_mid = await client.get(midpoint_url, params={"token_id": token_id})
                resp_mid.raise_for_status()
                mid_data = resp_mid.json()

                midpoint = float(mid_data.get("mid", 0)) if mid_data else 0

                # 使用 price 端点获取 bid/ask
                price_url = f"https://clob.polymarket.com/price"

                # 并发查询 buy 和 sell
                resp_buy = await client.get(price_url, params={"token_id": token_id, "side": "BUY"})
                resp_sell = await client.get(price_url, params={"token_id": token_id, "side": "SELL"})

                bid = 0
                ask = 0

                if resp_buy.status_code == 200:
                    buy_data = resp_buy.json()
                    bid = float(buy_data.get("price", 0))

                if resp_sell.status_code == 200:
                    sell_data = resp_sell.json()
                    ask = float(sell_data.get("price", 0))

                # 如果 bid/ask 都没有，使用 midpoint
                if not bid and not ask:
                    bid = ask = midpoint

                odds_data = {
                    "event_id": token_info["event_id"],
                    "token_id": token_id,
                    "outcome": token_info["outcome"],
                    "market_type": token_info["market_type"],
                    "bid": bid,
                    "ask": ask,
                    "last": midpoint or ((bid + ask) / 2 if bid and ask else 0),
                    "timestamp": int(asyncio.get_event_loop().time() * 1000),
                }

                # 更新缓存
                self._latest_odds[token_id] = odds_data

                # 触发回调
                if self._price_update_callback:
                    self._price_update_callback(odds_data)

        except Exception as e:
            self._log.debug(f"Failed to fetch price for token {token_id}: {e}")

    # =========================================================================
    # 回调管理
    # =========================================================================

    def on_price_update(self, callback: Callable[[dict], None]) -> None:
        """
        设置价格更新回调

        Args:
            callback: 回调函数，接收 odds_data 字典
        """
        self._price_update_callback = callback

    # =========================================================================
    # 数据访问
    # =========================================================================

    def get_latest_odds(self, event_id: str) -> dict[str, dict]:
        """
        获取 event 的最新赔率

        Args:
            event_id: event ID

        Returns:
            {market_type: odds_data}
        """
        result = {}

        for token_id, odds_data in self._latest_odds.items():
            if odds_data["event_id"] == event_id:
                market_type = odds_data["market_type"]
                result[market_type] = odds_data

        return result

    # =========================================================================
    # 生命周期
    # =========================================================================

    async def start(self) -> None:
        """启动客户端"""
        self._log.info("Polymarket odds client started")

    async def stop(self) -> None:
        """停止客户端"""
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        self._log.info("Polymarket odds client stopped")
