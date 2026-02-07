"""
Polymarket 赔率客户端

使用 Polymarket CLOB WebSocket 获取实时赔率数据。

WebSocket URL: wss://ws-subscriptions-clob.polymarket.com/ws/market
参考文档: https://docs.polymarket.com/developers/CLOB/websocket/market-channel
"""

import asyncio
import logging
import json
import time
from typing import Any, Callable

import httpx

try:
    import websockets
except ImportError:
    websockets = None


from .config import OddsSubscriptionConfig


class PolymarketOddsClient:
    """
    Polymarket 赔率客户端

    使用 WebSocket 实时接收赔率更新：
    - book: 订单簿快照
    - price_change: 价格变化
    - last_trade_price: 最新成交价
    """

    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    def __init__(
        self,
        config: OddsSubscriptionConfig,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self._log = logger or logging.getLogger(self.__class__.__name__)

        # WebSocket 连接
        self._ws = None
        self._ws_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None

        # 订阅管理
        self._subscribed_tokens: dict[str, dict] = {}  # token_id -> token_info
        self._pending_subscribe: list[str] = []  # 待订阅的 token_ids

        # 数据缓存
        self._latest_odds: dict[str, dict] = {}  # token_id -> odds_data

        # 回调函数
        self._price_update_callback: Callable[[dict], None] | None = None

        # 状态
        self._running = False

        # 锁：防止并行订阅时的竞态条件
        self._ws_lock = asyncio.Lock()

    # =========================================================================
    # API 查询
    # =========================================================================

    async def get_event_tokens(self, event_id: str) -> list[dict[str, Any]]:
        """
        获取 event 的所有 tokens

        使用 ticker 匹配逻辑：
        1. 从 event 获取 ticker，按 '-' 拆分，第二个值为 home_team，第三个值为 away_team
        2. 遍历 markets：
           - 如果 market ticker == event ticker：多结果市场，outcomes[0]=home, outcomes[1]=away
           - 如果 market ticker == event_ticker + "-" + home_team：主队胜，取 Yes
           - 如果 market ticker == event_ticker + "-" + away_team：客队胜，取 Yes
           - 如果 market ticker == event_ticker + "-draw"：平局，取 Yes

        Args:
            event_id: Polymarket event ID

        Returns:
            tokens 列表
        """
        self._log.info(f"Fetching tokens for event {event_id}")

        url = f"https://gamma-api.polymarket.com/events/{event_id}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                event_data = resp.json()

                tokens = []
                markets = event_data.get("markets", [])

                if not markets:
                    self._log.warning(f"No markets found for event {event_id}")
                    return tokens

                # 获取 event ticker 并解析队名
                event_ticker = event_data.get("ticker", "")
                event_title = event_data.get("title", "")

                # 解析 ticker: 格式如 "epl-not-lee-2025-11-09"
                # 第二个值 = home_team (not), 第三个值 = away_team (lee)
                ticker_parts = event_ticker.split("-") if event_ticker else []
                home_abbr = ticker_parts[1].lower() if len(ticker_parts) > 1 else ""
                away_abbr = ticker_parts[2].lower() if len(ticker_parts) > 2 else ""

                # 队名首字母大写
                home_team = home_abbr.capitalize() if home_abbr else ""
                away_team = away_abbr.capitalize() if away_abbr else ""

                self._log.info(
                    f"Event: {event_title}, ticker={event_ticker}, "
                    f"home={home_team}, away={away_team}, markets={len(markets)}"
                )

                # 构建期望的 market slug
                home_slug = f"{event_ticker}-{home_abbr}" if home_abbr else ""
                away_slug = f"{event_ticker}-{away_abbr}" if away_abbr else ""
                draw_slug = f"{event_ticker}-draw"

                for market in markets:
                    market_slug = market.get("slug", "")
                    outcomes_raw = market.get("outcomes", [])
                    clob_token_ids_raw = market.get("clobTokenIds", "[]")

                    # 解析 outcomes
                    try:
                        if isinstance(outcomes_raw, str):
                            outcomes = json.loads(outcomes_raw)
                        else:
                            outcomes = outcomes_raw
                    except json.JSONDecodeError:
                        outcomes = []

                    # 解析 clobTokenIds
                    try:
                        if isinstance(clob_token_ids_raw, str):
                            clob_token_ids = json.loads(clob_token_ids_raw)
                        else:
                            clob_token_ids = clob_token_ids_raw
                    except json.JSONDecodeError:
                        clob_token_ids = []

                    if not clob_token_ids:
                        continue

                    # 根据 ticker 匹配
                    if market_slug == event_ticker:
                        # 完全匹配：多结果市场，outcomes[0]=home, outcomes[1]=away
                        self._log.info(f"  Market ticker={market_slug} (exact match), outcomes={outcomes}")

                        if len(outcomes) >= 2 and len(clob_token_ids) >= 2:
                            # outcomes[0] = home win
                            tokens.append({
                                "token_id": clob_token_ids[0],
                                "outcome": outcomes[0],
                                "market_type": "home",
                                "event_id": event_id,
                                "ticker": market_slug,
                                "home_team": home_team,
                                "away_team": away_team,
                            })
                            # outcomes[1] = away win
                            tokens.append({
                                "token_id": clob_token_ids[1],
                                "outcome": outcomes[1],
                                "market_type": "away",
                                "event_id": event_id,
                                "ticker": market_slug,
                                "home_team": home_team,
                                "away_team": away_team,
                            })

                    elif home_slug and market_slug == home_slug:
                        # 主队胜市场
                        self._log.info(f"  Market ticker={market_slug} (home win)")
                        tokens.append({
                            "token_id": clob_token_ids[0],  # Yes token
                            "outcome": "Yes",
                            "market_type": "home",
                            "event_id": event_id,
                            "ticker": market_slug,
                            "home_team": home_team,
                            "away_team": away_team,
                        })

                    elif away_slug and market_slug == away_slug:
                        # 客队胜市场
                        self._log.info(f"  Market ticker={market_slug} (away win)")
                        tokens.append({
                            "token_id": clob_token_ids[0],  # Yes token
                            "outcome": "Yes",
                            "market_type": "away",
                            "event_id": event_id,
                            "ticker": market_slug,
                            "home_team": home_team,
                            "away_team": away_team,
                        })

                    elif market_slug == draw_slug:
                        # 平局市场
                        self._log.info(f"  Market ticker={market_slug} (draw)")
                        tokens.append({
                            "token_id": clob_token_ids[0],  # Yes token
                            "outcome": "Yes",
                            "market_type": "draw",
                            "event_id": event_id,
                            "ticker": market_slug,
                            "home_team": home_team,
                            "away_team": away_team,
                        })

                    else:
                        # 其他 ticker 忽略
                        self._log.debug(f"  Skipping market ticker={market_slug}")

                self._log.info(f"Found {len(tokens)} tokens for event {event_id}")
                return tokens

            except Exception as e:
                self._log.error(f"Failed to fetch tokens for event {event_id}: {e}")
                return []

    def _infer_market_type_from_question(self, question: str, event_title: str) -> str:
        """
        根据市场问题推断类型 (home/draw/away)

        Args:
            question: 市场问题，如 "Will Leeds United FC win on 2026-01-31?"
            event_title: 事件标题，如 "Leeds United FC vs. Arsenal FC"
        """
        question_lower = question.lower()

        # 检查是否是平局
        if "draw" in question_lower or "tie" in question_lower:
            return "draw"

        # 从 event_title 提取主客队名
        # 格式: "Team A vs. Team B" 或 "Team A vs Team B"
        teams = self._extract_teams_from_title(event_title)

        if teams and len(teams) >= 2:
            home_team = teams[0].lower()
            away_team = teams[1].lower()

            # 检查问题中包含哪个队名
            if home_team in question_lower:
                return "home"
            elif away_team in question_lower:
                return "away"

        # 默认根据问题内容猜测
        if "win" in question_lower:
            return "home"  # 默认为主队

        return "unknown"

    def _extract_teams_from_title(self, title: str) -> list[str]:
        """从标题提取队名"""
        import re

        # 处理带有赛事前缀的格式，如 "Australian Open Men's: Carlos Alcaraz vs Novak Djokovic"
        # 先去掉冒号前的赛事名称
        if ":" in title:
            # 取冒号后面的部分
            parts = title.split(":", 1)
            if len(parts) == 2:
                title = parts[1].strip()

        # 匹配 "Team A vs. Team B" 或 "Team A vs Team B"
        match = re.match(r"(.+?)\s+vs\.?\s+(.+)", title, re.IGNORECASE)
        if match:
            return [match.group(1).strip(), match.group(2).strip()]
        return []

    # =========================================================================
    # WebSocket 连接
    # =========================================================================

    async def _connect_websocket(self) -> None:
        """连接 WebSocket"""
        if websockets is None:
            self._log.error("websockets library not installed. Install with: pip install websockets")
            return

        self._log.info(f"Connecting to WebSocket: {self.WS_URL}")

        try:
            self._ws = await websockets.connect(
                self.WS_URL,
                ping_interval=30,
                ping_timeout=10,
            )
            self._log.info("WebSocket connected")

            # 订阅待订阅的 tokens
            if self._pending_subscribe:
                self._log.info(f"Sending {len(self._pending_subscribe)} pending token subscriptions")
                await self._send_subscribe(self._pending_subscribe)
                self._pending_subscribe = []
            else:
                self._log.info("No pending subscriptions to send")

        except Exception as e:
            self._log.error(f"WebSocket connection failed: {e}")
            self._ws = None
            raise

    async def _send_subscribe(self, token_ids: list[str]) -> None:
        """发送订阅消息"""
        if not self._ws:
            self._log.warning("WebSocket not connected, queuing subscription")
            self._pending_subscribe.extend(token_ids)
            return

        # 订阅消息格式
        subscribe_msg = {
            "assets_ids": token_ids,
            "type": "market",
        }

        await self._ws.send(json.dumps(subscribe_msg))
        self._log.info(f"Sent subscription for {len(token_ids)} tokens")

        # Debug: log the actual token IDs being subscribed
        for token_id in token_ids:
            token_info = self._subscribed_tokens.get(token_id, {})
            self._log.debug(
                f"  Subscribed token: event={token_info.get('event_id')}, "
                f"type={token_info.get('market_type')}, outcome={token_info.get('outcome')}"
            )

    async def _run_websocket(self) -> None:
        """WebSocket 主循环"""
        while self._running:
            try:
                # 连接
                await self._connect_websocket()

                # 接收消息循环
                async for message in self._ws:
                    if not self._running:
                        break

                    try:
                        data = json.loads(message)
                        await self._handle_message(data)
                    except json.JSONDecodeError:
                        self._log.warning(f"Invalid JSON message: {message[:100]}")
                    except Exception as e:
                        self._log.error(f"Error handling message: {e}")

            except websockets.exceptions.ConnectionClosed as e:
                self._log.warning(f"WebSocket connection closed: {e}")
            except Exception as e:
                self._log.error(f"WebSocket error: {e}")

            # 重连
            if self._running:
                self._log.info("Reconnecting in 5 seconds...")
                await asyncio.sleep(5)

    async def _handle_message(self, data: Any) -> None:
        """
        处理 WebSocket 消息

        消息类型:
        - book: 订单簿快照
        - price_change: 价格变化
        - last_trade_price: 最新成交价

        注意：消息可能是单个 dict 或 list 格式
        """
        # 如果是列表，遍历处理每个消息
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    await self._handle_single_message(item)
            return

        # 单个消息
        if isinstance(data, dict):
            await self._handle_single_message(data)

    async def _handle_single_message(self, data: dict) -> None:
        """
        处理单个 WebSocket 消息

        Polymarket 消息格式（实际观测）：
        1. 订单簿快照：有 bids/asks 数组，asset_id 在顶层
        2. 价格变化：有 price_changes 数组，每项包含 asset_id, best_bid, best_ask
        """
        timestamp = int(time.time() * 1000)

        # 类型1：订单簿快照
        # {"market": "...", "asset_id": "...", "bids": [...], "asks": [...]}
        if "bids" in data and "asks" in data:
            asset_id = data.get("asset_id", "")
            if asset_id not in self._subscribed_tokens:
                self._log.debug(f"Book message for unsubscribed token: {asset_id[:30]}...")
                return

            token_info = self._subscribed_tokens[asset_id]
            bids = data.get("bids", [])
            asks = data.get("asks", [])

            # 取最优价格：最高 bid，最低 ask
            # 订单簿可能不按价格排序，需要遍历查找
            best_bid = max((float(b["price"]) for b in bids), default=0) if bids else 0
            best_ask = min((float(a["price"]) for a in asks), default=0) if asks else 0

            odds_data = {
                "event_id": token_info["event_id"],
                "token_id": asset_id,
                "outcome": token_info["outcome"],
                "market_type": token_info["market_type"],
                "home_team": token_info.get("home_team", ""),
                "away_team": token_info.get("away_team", ""),
                "bid": best_bid,
                "ask": best_ask,
                "last": (best_bid + best_ask) / 2 if best_bid and best_ask else 0,
                "timestamp": timestamp,
                "source": "book",
            }

            self._update_odds(asset_id, odds_data)
            return

        # 类型2：价格变化
        # {"market": "...", "price_changes": [{"asset_id": "...", "best_bid": "0.63", "best_ask": "0.64", ...}, ...]}
        if "price_changes" in data:
            price_changes = data.get("price_changes", [])

            for change in price_changes:
                asset_id = change.get("asset_id", "")
                if asset_id not in self._subscribed_tokens:
                    self._log.debug(f"Price change for unsubscribed token: {asset_id[:30]}...")
                    continue

                token_info = self._subscribed_tokens[asset_id]

                # 从 price_changes 获取 best_bid/best_ask
                best_bid_str = change.get("best_bid", "0")
                best_ask_str = change.get("best_ask", "0")

                best_bid = float(best_bid_str) if best_bid_str else 0
                best_ask = float(best_ask_str) if best_ask_str else 0

                odds_data = {
                    "event_id": token_info["event_id"],
                    "token_id": asset_id,
                    "outcome": token_info["outcome"],
                    "market_type": token_info["market_type"],
                    "home_team": token_info.get("home_team", ""),
                    "away_team": token_info.get("away_team", ""),
                    "bid": best_bid,
                    "ask": best_ask,
                    "last": (best_bid + best_ask) / 2 if best_bid and best_ask else 0,
                    "timestamp": timestamp,
                    "source": "price_change",
                }

                self._update_odds(asset_id, odds_data)
            return

        # 类型3：传统 event_type 格式（兼容旧格式）
        event_type = data.get("event_type")
        asset_id = data.get("asset_id")

        if not event_type or not asset_id:
            return

        if asset_id not in self._subscribed_tokens:
            return

        token_info = self._subscribed_tokens[asset_id]

        if event_type == "last_trade_price":
            price = float(data.get("price", 0))

            existing = self._latest_odds.get(asset_id, {})
            existing.update({
                "event_id": token_info["event_id"],
                "token_id": asset_id,
                "outcome": token_info["outcome"],
                "market_type": token_info["market_type"],
                "last": price,
                "timestamp": timestamp,
                "source": "last_trade",
            })

            self._update_odds(asset_id, existing)

    def _update_odds(self, token_id: str, odds_data: dict) -> None:
        """更新赔率并触发回调"""
        self._latest_odds[token_id] = odds_data

        if self._price_update_callback:
            self._price_update_callback(odds_data)

    # =========================================================================
    # 订阅管理
    # =========================================================================

    async def subscribe_event(self, event_id: str) -> None:
        """
        订阅 event 的所有 tokens

        Args:
            event_id: Polymarket event ID
        """
        self._log.info(f"Starting subscription for event {event_id}")

        # 1. 获取 tokens（可以并行执行，不需要锁）
        tokens = await self.get_event_tokens(event_id)

        if not tokens:
            self._log.warning(f"No tokens to subscribe for event {event_id}")
            return

        self._log.info(f"Got {len(tokens)} tokens for event {event_id}")
        for t in tokens:
            self._log.info(f"  Token: type={t['market_type']}, outcome={t['outcome']}, id={t['token_id'][:20]}...")

        # 2. 使用锁保护订阅和 WebSocket 启动
        async with self._ws_lock:
            # 记录订阅
            token_ids = []
            for token_info in tokens:
                token_id = token_info["token_id"]
                self._subscribed_tokens[token_id] = token_info
                token_ids.append(token_id)

            self._log.info(f"Total subscribed tokens now: {len(self._subscribed_tokens)}")

            # 发送订阅
            await self._send_subscribe(token_ids)

            # 启动 WebSocket（如果还未启动）
            if not self._ws_task or self._ws_task.done():
                self._log.info("Starting WebSocket task")
                self._ws_task = asyncio.create_task(self._run_websocket())

        self._log.info(f"Subscribed to {len(tokens)} tokens for event {event_id}")

    # =========================================================================
    # 回调管理
    # =========================================================================

    def on_price_update(self, callback: Callable[[dict], None]) -> None:
        """设置价格更新回调"""
        self._price_update_callback = callback

    # =========================================================================
    # 数据访问
    # =========================================================================

    def clear_subscriptions(self) -> None:
        """
        清除所有订阅数据（用于重新订阅前）
        """
        old_count = len(self._subscribed_tokens)
        self._subscribed_tokens.clear()
        self._latest_odds.clear()
        self._pending_subscribe.clear()
        self._log.info(f"Cleared {old_count} token subscriptions")

    def get_latest_odds(self, event_id: str) -> dict[str, dict]:
        """获取 event 的最新赔率"""
        result = {}

        for token_id, odds_data in self._latest_odds.items():
            if odds_data.get("event_id") == event_id:
                market_type = odds_data.get("market_type", "unknown")
                result[market_type] = odds_data

        return result

    # =========================================================================
    # 生命周期
    # =========================================================================

    async def start(self) -> None:
        """启动客户端"""
        self._running = True
        self._log.info("Polymarket odds client started")

    async def stop(self) -> None:
        """停止客户端"""
        self._running = False

        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.close()
            self._ws = None

        self._log.info("Polymarket odds client stopped")
