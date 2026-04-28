"""
Polymarket 赔率客户端

使用 Polymarket CLOB WebSocket 获取实时赔率数据。

WebSocket URLs:
- Market Channel: wss://ws-subscriptions-clob.polymarket.com/ws/market (公开)
- User Channel: wss://ws-subscriptions-clob.polymarket.com/ws/user (需认证)

参考文档:
- https://docs.polymarket.com/developers/CLOB/websocket/market-channel
- https://docs.polymarket.com/developers/CLOB/websocket/user-channel
"""

import asyncio
import logging
import json
import time
import hmac
import hashlib
import base64
from typing import Any, Callable
from dataclasses import dataclass, field

import httpx

try:
    import websockets
except ImportError:
    websockets = None

# 尝试导入 py-clob-client
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    HAS_CLOB_CLIENT = True
except ImportError:
    HAS_CLOB_CLIENT = False
    ClobClient = None

from src.arbitrage.common.subscription_config import OddsSubscriptionConfig


@dataclass
class PolymarketOrder:
    """
    Polymarket 订单数据

    从 User Channel WebSocket 消息解析
    """
    order_id: str
    market: str  # condition_id
    asset_id: str  # token_id
    side: str  # "BUY" or "SELL"
    price: float
    original_size: float
    size_matched: float
    status: str  # "LIVE", "MATCHED", "CANCELLED"
    outcome: str = ""  # "Yes" or "No"
    event_id: str = ""
    timestamp: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "PolymarketOrder":
        """从 User Channel 消息解析"""
        return cls(
            order_id=data.get("id", ""),
            market=data.get("market", ""),
            asset_id=data.get("asset_id", ""),
            side=data.get("side", ""),
            price=float(data.get("price", 0)),
            original_size=float(data.get("original_size", 0)),
            size_matched=float(data.get("size_matched", 0)),
            status=data.get("status", ""),
            outcome=data.get("outcome", ""),
            timestamp=int(data.get("timestamp", 0)),
        )


@dataclass
class PolymarketPosition:
    """
    Polymarket 持仓数据

    从持仓汇总计算得出
    """
    asset_id: str  # token_id
    condition_id: str  # market condition ID
    outcome: str  # "Yes" or "No"
    market_type: str  # "home", "away", "draw"
    size: float  # 持仓数量
    avg_price: float  # 平均成本
    current_price: float  # 当前价格
    event_id: str = ""
    neg_risk: bool = False  # 是否为负风险市场
    redeemable: bool = False  # Data API 返回的可赎回标记
    mergeable: bool = False  # Data API 返回的可合并标记

    @property
    def profit_if_win(self) -> float:
        """如果该 outcome 赢时的盈利"""
        # 赢时获得 size * 1.0，减去成本
        return self.size * (1.0 - self.avg_price)

    @property
    def loss_if_lose(self) -> float:
        """如果该 outcome 输时的亏损"""
        # 输时获得 0，损失成本
        return self.size * self.avg_price


class PolymarketOddsClient:
    """
    Polymarket 赔率客户端

    使用 WebSocket 实时接收赔率更新：
    - Market Channel: 订单簿快照、价格变化
    - User Channel: 订单更新、成交更新（需认证）
    """

    WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    WS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

    def __init__(
        self,
        config: OddsSubscriptionConfig,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self._log = logger or logging.getLogger(self.__class__.__name__)

        # Market Channel WebSocket 连接
        self._ws = None
        self._ws_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None

        # User Channel WebSocket 连接
        self._user_ws = None
        self._user_ws_task: asyncio.Task | None = None

        # 订阅管理
        self._subscribed_tokens: dict[str, dict] = {}  # token_id -> token_info
        self._pending_subscribe: list[str] = []  # 待订阅的 token_ids

        # 数据缓存
        self._latest_odds: dict[str, dict] = {}  # token_id -> odds_data

        # 订单数据（来自 User Channel）
        # 用于记录未完全成交订单
        self._current_orders: dict[str, PolymarketOrder] = {}  # order_id -> order
        self._positions: dict[str, PolymarketPosition] | None = None  # asset_id -> position
        self._positions_event = asyncio.Event()

        # 回调函数
        self._price_update_callback: Callable[[dict], None] | None = None
        self._orders_update_callback: Callable[[dict], None] | None = None
        self._positions_update_callback: Callable[[str], None] | None = None  # event_id

        # 已处理的 CONFIRMED trade ID（去重）
        self._confirmed_trade_ids: set[str] = set()

        # 状态
        self._running = False

        # 锁：防止并行订阅时的竞态条件
        self._ws_lock = asyncio.Lock()
        # 统一锁：序列化所有 Polymarket API 调用（REST + ClobClient）
        self._api_lock = asyncio.Lock()

        # ClobClient 实例（由 ExecutionService 通过 initialize_clob_client 初始化）
        self._clob_client: ClobClient | None = None

    # =========================================================================
    # 统一 API 执行封装
    # =========================================================================

    async def _call_api(self, coro_or_func, *args, **kwargs):
        """
        统一 API 执行封装，所有 Polymarket API 调用都通过此方法。
        内部加锁，各操作函数不需要单独做同步。

        支持两种调用方式：
        - 协程: await _call_api(some_async_func, arg1, arg2)
        - 同步函数(ClobClient): await _call_api(self._clob_client.create_order, args)
          → 自动用 asyncio.to_thread 包装
        """
        async with self._api_lock:
            if asyncio.iscoroutinefunction(coro_or_func):
                return await coro_or_func(*args, **kwargs)
            else:
                # ClobClient 的同步方法，用线程池执行
                return await asyncio.to_thread(coro_or_func, *args, **kwargs)

    def initialize_clob_client(self) -> bool:
        """
        用 OddsSubscriptionConfig 初始化 ClobClient

        Returns:
            是否初始化成功
        """
        if self._clob_client is not None:
            return True

        if not HAS_CLOB_CLIENT:
            self._log.error("py-clob-client not installed")
            return False

        if not self.config.polymarket_private_key:
            self._log.error("Polymarket private key not configured")
            return False

        try:
            funder = self.config.polymarket_funder or None
            self._clob_client = ClobClient(
                host=self.config.polymarket_clob_url,
                key=self.config.polymarket_private_key,
                chain_id=137,  # Polygon mainnet
                signature_type=2,
                funder=funder,
            )
            self._log.info(f"ClobClient initialized: signature_type=2, funder={funder}")

            self._clob_client.set_api_creds(ApiCreds(
                api_key=self.config.polymarket_clob_api_key,
                api_secret=self.config.polymarket_clob_api_secret,
                api_passphrase=self.config.polymarket_clob_passphrase,
            ))

            self._log.info("ClobClient credentials set")
            return True

        except Exception as e:
            self._log.error(f"Failed to initialize ClobClient: {e}")
            return False

    async def create_and_post_order(self, order_args, order_type) -> dict:
        """创建并提交限价订单"""
        signed = await self._call_api(self._clob_client.create_order, order_args)
        return await self._call_api(self._clob_client.post_order, signed, order_type)

    async def create_and_post_market_order(self, market_order_args, order_type) -> dict:
        """创建并提交市价订单"""
        signed = await self._call_api(self._clob_client.create_market_order, market_order_args)
        return await self._call_api(self._clob_client.post_order, signed, order_type)

    async def cancel_clob_order(self, venue_order_id: str) -> dict:
        """撤销订单"""
        return await self._call_api(self._clob_client.cancel, venue_order_id)

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

                # event 级别的 negRisk 标志
                event_neg_risk = event_data.get("negRisk", False)

                for market in markets:
                    market_slug = market.get("slug", "")
                    market_condition_id = market.get("conditionId", "")
                    market_neg_risk = market.get("negRisk", event_neg_risk)
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
                                "condition_id": market_condition_id,
                                "neg_risk": market_neg_risk,
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
                                "condition_id": market_condition_id,
                                "neg_risk": market_neg_risk,
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
                            "condition_id": market_condition_id,
                            "neg_risk": market_neg_risk,
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
                            "condition_id": market_condition_id,
                            "neg_risk": market_neg_risk,
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
                            "condition_id": market_condition_id,
                            "neg_risk": market_neg_risk,
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
        """连接 Market Channel WebSocket"""
        if websockets is None:
            self._log.error("websockets library not installed. Install with: pip install websockets")
            return

        self._log.info(f"Connecting to Market Channel WebSocket: {self.WS_MARKET_URL}")

        try:
            self._ws = await websockets.connect(
                self.WS_MARKET_URL,
                ping_interval=30,
                ping_timeout=10,
            )
            self._log.info("Market Channel WebSocket connected")

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
        # custom_feature_enabled=true 启用 best_bid_ask 消息，减少不必要的消息量
        # 参考: https://docs.polymarket.com/developers/CLOB/websocket/wss-overview
        subscribe_msg = {
            "assets_ids": token_ids,
            "type": "market",
            "custom_feature_enabled": True,  # 启用 best_bid_ask 消息
        }

        await self._ws.send(json.dumps(subscribe_msg))
        self._log.info(f"Sent subscription for {len(token_ids)} tokens (custom_feature_enabled=true)")

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
                        import traceback
                        traceback.print_exc()

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

            # 取最优价格及其可用数量：最高 bid，最低 ask
            # 订单簿可能不按价格排序，需要遍历查找
            best_bid_entry = max(bids, key=lambda b: float(b["price"])) if bids else None
            best_ask_entry = min(asks, key=lambda a: float(a["price"])) if asks else None

            best_bid = float(best_bid_entry["price"]) if best_bid_entry else 0
            best_ask = float(best_ask_entry["price"]) if best_ask_entry else 0
            bid_size = float(best_bid_entry.get("size", 0)) if best_bid_entry else 0
            ask_size = float(best_ask_entry.get("size", 0)) if best_ask_entry else 0

            odds_data = {
                "event_id": token_info["event_id"],
                "token_id": asset_id,
                "outcome": token_info["outcome"],
                "market_type": token_info["market_type"],
                "home_team": token_info.get("home_team", ""),
                "away_team": token_info.get("away_team", ""),
                "bid": best_bid,
                "ask": best_ask,
                "bid_size": bid_size,
                "ask_size": ask_size,
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

        # 类型3：event_type 格式（best_bid_ask, last_trade_price 等）
        event_type = data.get("event_type")
        asset_id = data.get("asset_id")

        if not event_type or not asset_id:
            return

        if asset_id not in self._subscribed_tokens:
            return

        token_info = self._subscribed_tokens[asset_id]

        # best_bid_ask: 最优价格变化（启用 custom_feature_enabled 后收到）
        # 这是最高效的消息类型，只在最优价格变化时触发
        if event_type == "best_bid_ask":
            best_bid = float(data.get("best_bid", 0))
            best_ask = float(data.get("best_ask", 0))

            odds_data = {
                "event_id": token_info["event_id"],
                "token_id": asset_id,
                "outcome": token_info["outcome"],
                "market_type": token_info["market_type"],
                "home_team": token_info.get("home_team", ""),
                "away_team": token_info.get("away_team", ""),
                "bid": best_bid,
                "ask": best_ask,
                "spread": float(data.get("spread", 0)),
                "last": (best_bid + best_ask) / 2 if best_bid and best_ask else 0,
                "timestamp": int(data.get("timestamp", timestamp)),
                "source": "best_bid_ask",
            }

            self._update_odds(asset_id, odds_data)

        elif event_type == "last_trade_price":
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
    # User Channel WebSocket (订单更新) + REST API (持仓查询)
    # =========================================================================

    # Polymarket API 架构：
    # - Market Channel WebSocket: 赔率数据（公开）
    # - User Channel WebSocket: 订单/成交实时更新（需认证）
    # - Data API REST: 持仓查询（需认证）
    #
    # 参考: https://docs.polymarket.com/developers/CLOB/websocket

    DATA_API_URL = "https://data-api.polymarket.com"
    CLOB_API_URL = "https://clob.polymarket.com"

    def _build_hmac_signature(self, secret_raw: str, timestamp: str, method: str, path: str, body: str = "") -> str:
        """
        构建 HMAC-SHA256 签名

        与 py-clob-client 和 py-builder-signing-sdk 保持一致:
        - message = timestamp + method + requestPath + body
        - secret 使用 urlsafe_b64decode
        - 签名使用 urlsafe_b64encode
        """
        secret = base64.urlsafe_b64decode(secret_raw)
        message = f"{timestamp}{method}{path}"
        if body:
            message += body
        sig = hmac.new(secret, message.encode(), hashlib.sha256)
        return base64.urlsafe_b64encode(sig.digest()).decode()

    def _generate_l1_auth_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """
        生成 Polymarket Data API 认证 headers

        用于 data-api.polymarket.com 端点 (如 /positions)。
        Data API 使用毫秒时间戳。
        """
        if not self.config.polymarket_clob_api_key or not self.config.polymarket_clob_api_secret:
            return {}

        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method}{path}{body}"

        try:
            secret_raw = self.config.polymarket_clob_api_secret.strip()
            padding = (-len(secret_raw)) % 4
            if padding:
                secret_raw += "=" * padding
            secret = base64.urlsafe_b64decode(secret_raw)
            signature = hmac.new(secret, message.encode(), hashlib.sha256)
            signature_b64 = base64.b64encode(signature.digest()).decode()
        except Exception as e:
            self._log.error(f"Failed to generate L1 signature: {e}")
            return {}

        return {
            "POLY_ADDRESS": self.config.polymarket_clob_api_key,
            "POLY_SIGNATURE": signature_b64,
            "POLY_TIMESTAMP": timestamp,
            "POLY_PASSPHRASE": self.config.polymarket_clob_passphrase,
        }

    def _generate_clob_auth_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """
        生成 Polymarket CLOB API L2 认证 headers

        用于 clob.polymarket.com 端点 (如 /data/orders)。
        与 py-clob-client create_level_2_headers 保持一致:
        - POLY_ADDRESS = EOA 钱包地址
        - POLY_API_KEY = API key UUID
        - POLY_TIMESTAMP = 秒级时间戳
        - POLY_SIGNATURE = urlsafe_b64encode HMAC
        - POLY_PASSPHRASE = passphrase
        """
        if not self.config.polymarket_clob_api_key or not self.config.polymarket_clob_api_secret:
            return {}

        eoa_address = self.config.polymarket_eoa_address
        if not eoa_address:
            self._log.error("EOA address not configured for CLOB auth")
            return {}

        timestamp = str(int(time.time()))

        try:
            signature_b64 = self._build_hmac_signature(
                self.config.polymarket_clob_api_secret, timestamp, method, path, body
            )
        except Exception as e:
            self._log.error(f"Failed to generate CLOB signature: {e}")
            return {}

        return {
            "POLY_ADDRESS": eoa_address,
            "POLY_SIGNATURE": signature_b64,
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": self.config.polymarket_clob_api_key,
            "POLY_PASSPHRASE": self.config.polymarket_clob_passphrase,
        }

    async def fetch_positions(self) -> list[PolymarketPosition] | None:
        """
        从 Data API 获取用户持仓

        通过 _call_api 统一加锁，防止与其他 API 调用并发冲突。

        Returns:
            持仓列表
        """
        if not self.config.polymarket_clob_api_key:
            self._log.debug("No API key configured, skipping position fetch")
            return []

        user_address = self.config.polymarket_user_address
        if not user_address and self.config.polymarket_clob_api_key.startswith("0x"):
            user_address = self.config.polymarket_clob_api_key

        if not user_address:
            self._log.warning(
                "No Polymarket user address configured, skipping position fetch"
            )
            return []

        return await self._call_api(self._do_fetch_positions, user_address)

    async def _do_fetch_positions(self, user_address: str) -> list[PolymarketPosition] | None:
        """实际执行 fetch positions（在 _call_api 锁内调用）"""
        path = "/positions"
        headers = self._generate_l1_auth_headers("GET", path)

        if not headers:
            return []

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self.DATA_API_URL}{path}",
                    headers=headers,
                    params={"user": user_address},
                )
                resp.raise_for_status()
                data = resp.json()

                positions = []
                self._log.debug(f"_subscribed_tokens keys: {[k[:20]+'...' for k in self._subscribed_tokens.keys()]}")
                for item in data:
                    asset_id = item.get("asset", item.get("asset_id", ""))
                    token_info = self._subscribed_tokens.get(asset_id, {})
                    self._log.debug(
                        f"Position asset_id={asset_id[:20]}..., "
                        f"token_match={'YES' if token_info else 'NO'}, "
                        f"event_id={token_info.get('event_id', '')}, "
                        f"market_type={token_info.get('market_type', '')}, "
                        f"size={item.get('size', 0)}, "
                        f"raw_keys={list(item.keys())}"
                    )

                    # 获取当前价格
                    current_price = 0.0
                    if asset_id in self._latest_odds:
                        current_price = self._latest_odds[asset_id].get("bid", 0)

                    pos = PolymarketPosition(
                        asset_id=asset_id,
                        condition_id=token_info.get("condition_id", item.get("conditionId", item.get("condition_id", ""))),
                        outcome=token_info.get("outcome", item.get("outcome", "")),
                        market_type=token_info.get("market_type", ""),
                        size=float(item.get("size", 0)),
                        avg_price=float(item.get("avgPrice", item.get("avg_price", 0))),
                        current_price=current_price,
                        event_id=token_info.get("event_id", str(item.get("eventId", item.get("eventSlug", "")))),
                        neg_risk=token_info.get("neg_risk", item.get("negativeRisk", False)),
                        redeemable=bool(item.get("redeemable", False)),
                        mergeable=bool(item.get("mergeable", False)),
                    )
                    positions.append(pos)

                # 用 API 返回的数据完整替换缓存（merge 后不在列表中的仓位会被移除）
                new_positions = {}
                for pos in positions:
                    if pos.size > 0:
                        new_positions[pos.asset_id] = pos
                self._positions = new_positions

                self._log.info(f"Fetched {len(positions)} positions from Data API")
                self._positions_event.set()

                # 触发仓位更新回调
                if self._positions_update_callback:
                    updated_events = set(p.event_id for p in positions if p.event_id)
                    for event_id in updated_events:
                        try:
                            self._positions_update_callback(event_id)
                        except Exception as e:
                            self._log.error(f"Positions callback error for {event_id}: {e}")

                return positions

        except Exception as e:
            self._log.error(f"Failed to fetch positions: {type(e).__name__}: {e}")
            return None

    async def fetch_open_orders(self, asset_id: str | None = None) -> list[PolymarketOrder]:
        """
        从 CLOB API 获取当前活跃订单

        通过 _call_api 统一加锁，防止与其他 API 调用并发冲突。

        Args:
            asset_id: 可选，按 asset_id 过滤

        Returns:
            订单列表
        """
        if not self.config.polymarket_clob_api_key:
            self._log.debug("No API key configured, skipping orders fetch")
            return []

        return await self._call_api(self._do_fetch_open_orders, asset_id)

    async def _do_fetch_open_orders(self, asset_id: str | None = None) -> list[PolymarketOrder]:
        """实际执行 fetch open orders（在 _call_api 锁内调用）"""
        path = "/data/orders"
        headers = self._generate_clob_auth_headers("GET", path)

        if not headers:
            return []

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                params = {}
                if asset_id:
                    params["asset_id"] = asset_id
                resp = await client.get(
                    f"{self.CLOB_API_URL}{path}",
                    headers=headers,
                    params=params,
                )
                resp.raise_for_status()
                result = resp.json()

                # CLOB API 返回 {"data": [...], "next_cursor": ..., "count": ...}
                items = result.get("data", result) if isinstance(result, dict) else result

                orders = []
                for item in items:
                    order = PolymarketOrder.from_dict(item)
                    orders.append(order)
                    # 同步更新缓存
                    if order.status in ("CANCELLED", "MATCHED"):
                        self._current_orders.pop(order.order_id, None)
                    else:
                        self._current_orders[order.order_id] = order

                self._log.info(f"Fetched {len(orders)} orders from CLOB API")
                return orders

        except Exception as e:
            self._log.error(f"Failed to fetch open orders: {e}")
            return []

    async def _connect_user_websocket(self) -> None:
        """连接 User Channel WebSocket"""
        if websockets is None:
            self._log.error("websockets library not installed")
            return

        if not self.config.polymarket_clob_api_key:
            self._log.warning("Polymarket API key not configured, skipping User Channel")
            return

        self._log.info(f"Connecting to User Channel WebSocket: {self.WS_USER_URL}")

        try:
            # User Channel 使用 L1 认证
            auth_headers = self._generate_l1_auth_headers("GET", "/ws/user")

            self._user_ws = await websockets.connect(
                self.WS_USER_URL,
                additional_headers=auth_headers,
                ping_interval=30,
                ping_timeout=10,
            )
            self._log.info("User Channel WebSocket connected")

            # 连接成功后发送订阅消息
            await self._subscribe_user_channel()

            # 连接成功后获取初始持仓
            # 活跃订单由 User Channel WS 推送填充，无需单独 REST 查询
            await self.fetch_positions()

        except Exception as e:
            self._log.error(f"User Channel WebSocket connection failed: {e}")
            self._user_ws = None
            raise

    async def _subscribe_user_channel(self) -> None:
        """
        发送 User Channel 订阅消息

        从 _subscribed_tokens 收集唯一 condition_id 列表，
        发送订阅消息以接收对应 market 的 order/trade 事件。

        订阅消息格式（参考: https://docs.polymarket.com/developers/CLOB/websocket/user-channel）:
        {
          "auth": {
            "apiKey": "your-api-key",
            "secret": "your-api-secret",
            "passphrase": "your-passphrase"
          },
          "markets": ["condition_id"],
          "type": "user"
        }
        """
        if not self._user_ws:
            return

        # 收集唯一的 condition_id
        condition_ids = set()
        for token_info in self._subscribed_tokens.values():
            cid = token_info.get("condition_id", "")
            if cid:
                condition_ids.add(cid)

        if not condition_ids:
            self._log.info("No condition_ids to subscribe for User Channel")
            return

        # 验证凭证
        if not self.config.polymarket_clob_api_secret or not self.config.polymarket_clob_passphrase:
            self._log.error(
                f"User Channel subscription requires valid credentials: "
                f"secret={'SET' if self.config.polymarket_clob_api_secret else 'MISSING'}, "
                f"passphrase={'SET' if self.config.polymarket_clob_passphrase else 'MISSING'}"
            )
            return

        subscribe_msg = {
            "auth": {
                "apiKey": self.config.polymarket_clob_api_key,
                "secret": self.config.polymarket_clob_api_secret,
                "passphrase": self.config.polymarket_clob_passphrase,
            },
            "markets": list(condition_ids),
            "type": "user",
        }

        try:
            msg_json = json.dumps(subscribe_msg)
            self._log.info(
                f"Sending user channel subscription: markets={list(condition_ids)[:3]}{'...' if len(condition_ids) > 3 else ''}, "
                f"apiKey={self.config.polymarket_clob_api_key[:8] if self.config.polymarket_clob_api_key else 'NONE'}..., "
                f"secret_len={len(self.config.polymarket_clob_api_secret) if self.config.polymarket_clob_api_secret else 0}, "
                f"passphrase_len={len(self.config.polymarket_clob_passphrase) if self.config.polymarket_clob_passphrase else 0}"
            )
            await self._user_ws.send(msg_json)
            self._log.info(
                f"Sent user channel subscription for {len(condition_ids)} markets"
            )
        except Exception as e:
            self._log.error(f"Failed to send user channel subscription: {e}")

    async def _run_user_websocket(self) -> None:
        """User Channel WebSocket 主循环"""
        while self._running:
            try:
                await self._connect_user_websocket()

                if not self._user_ws:
                    self._log.warning("User Channel not available, stopping loop")
                    break

                async for message in self._user_ws:
                    if not self._running:
                        break

                    try:
                        data = json.loads(message)
                        await self._handle_user_message(data)
                    except json.JSONDecodeError:
                        self._log.warning(f"Invalid User Channel message: {message[:100]}")
                    except Exception as e:
                        self._log.error(f"Error handling User Channel message: {e}")

            except websockets.exceptions.ConnectionClosed as e:
                close_code = e.code if hasattr(e, 'code') else 'unknown'
                close_reason = e.reason if hasattr(e, 'reason') else 'unknown'
                self._log.warning(
                    f"User Channel connection closed: code={close_code}, "
                    f"reason='{close_reason}', exception={e}"
                )
            except Exception as e:
                self._log.error(f"User Channel error: {e}")

            # 重连
            if self._running:
                self._log.info("Reconnecting User Channel in 5 seconds...")
                await asyncio.sleep(5)

    async def _handle_user_message(self, data: Any) -> None:
        """
        处理 User Channel 消息

        消息类型:
        - order: 订单更新
        - trade: 成交更新
        """
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    await self._handle_single_user_message(item)
            return

        if isinstance(data, dict):
            await self._handle_single_user_message(data)

    async def _handle_single_user_message(self, data: dict) -> None:
        """
        处理单个 User Channel 消息

        Polymarket User Channel 消息为扁平结构：

        Order 消息:
        {
            "event_type": "order",
            "type": "PLACEMENT",  # PLACEMENT / UPDATE / CANCELLATION
            "id": "0xff354cd7...",
            "original_size": "10",
            "size_matched": "0",
            "asset_id": "...",
            "market": "...",
            "side": "SELL",
            "price": "0.57"
        }

        Trade 消息:
        {
            "event_type": "trade",
            "type": "TRADE",
            "id": "28c4d2eb-...",
            "status": "MATCHED",  # MATCHED → MINED → CONFIRMED / FAILED
            "maker_orders": [{"order_id": "...", "matched_amount": "10", ...}],
            "taker_order_id": "0x06bc63e...",
            "size": "10",
            "price": "0.57"
        }
        """
        event_type = data.get("event_type")

        self._log.debug(f"User Channel message: event_type={event_type}, data={data}")

        if event_type == "order":
            # 扁平结构：data 本身就是 order 数据
            await self._process_order_update(data)

        elif event_type == "trade":
            # 扁平结构：data 本身就是 trade 数据
            await self._process_trade_update(data)

        # 批量订单更新（初始连接时推送所有活跃订单）
        elif "orders" in data:
            orders = data.get("orders", [])
            self._log.info(f"Received {len(orders)} initial orders")
            for order_data in orders:
                await self._process_order_update(order_data)

    async def _process_order_update(self, order_data: dict) -> None:
        """
        处理订单更新（扁平格式）

        字段映射：
        - id → order_id
        - type → PLACEMENT / UPDATE / CANCELLATION
        - original_size → 原始数量（字符串）
        - size_matched → 累计成交量（字符串）
        - asset_id → token_id
        - market → condition_id
        - side → BUY / SELL
        - price → 价格

        Args:
            order_data: 订单数据（扁平结构）
        """
        try:
            order_id = order_data.get("id", "")
            if not order_id:
                return

            order_type = order_data.get("type", "")  # PLACEMENT / UPDATE / CANCELLATION
            asset_id = order_data.get("asset_id", "")
            token_info = self._subscribed_tokens.get(asset_id, {})

            original_size = float(order_data.get("original_size", 0))
            size_matched = float(order_data.get("size_matched", 0))

            self._log.info(
                f"Order {order_type}: id={order_id[:20]}..., "
                f"matched={size_matched}/{original_size}"
            )

            # 根据 order_type 更新活跃订单
            if order_type == "CANCELLATION":
                self._current_orders.pop(order_id, None)
                self._log.debug(f"Order removed (CANCELLATION): {order_id}")
            elif order_type == "PLACEMENT":
                order = PolymarketOrder(
                    order_id=order_id,
                    market=order_data.get("market", ""),
                    asset_id=asset_id,
                    side=order_data.get("side", ""),
                    price=float(order_data.get("price", 0)),
                    original_size=original_size,
                    size_matched=size_matched,
                    status="LIVE",
                    outcome=token_info.get("outcome", ""),
                    event_id=token_info.get("event_id", ""),
                    timestamp=int(time.time() * 1000),
                )
                self._current_orders[order_id] = order
                self._log.debug(f"Order added (PLACEMENT): {order_id}")
            elif order_type == "UPDATE":
                existing = self._current_orders.get(order_id)
                if existing:
                    existing.size_matched = size_matched
                    self._log.debug(
                        f"Order updated (UPDATE): {order_id}, "
                        f"size_matched={size_matched}"
                    )
                else:
                    # 缓存中没有，可能是重连后收到的，创建新记录
                    order = PolymarketOrder(
                        order_id=order_id,
                        market=order_data.get("market", ""),
                        asset_id=asset_id,
                        side=order_data.get("side", ""),
                        price=float(order_data.get("price", 0)),
                        original_size=original_size,
                        size_matched=size_matched,
                        status="LIVE",
                        outcome=token_info.get("outcome", ""),
                        event_id=token_info.get("event_id", ""),
                        timestamp=int(time.time() * 1000),
                    )
                    self._current_orders[order_id] = order
                    self._log.debug(f"Order added (UPDATE, not in cache): {order_id}")

            # 触发回调（通知 tracker）
            if self._orders_update_callback:
                # 构建 order 对象用于回调（CANCELLATION 时用临时对象）
                cb_order = self._current_orders.get(order_id)
                if not cb_order:
                    cb_order = PolymarketOrder(
                        order_id=order_id,
                        market=order_data.get("market", ""),
                        asset_id=asset_id,
                        side=order_data.get("side", ""),
                        price=float(order_data.get("price", 0)),
                        original_size=original_size,
                        size_matched=size_matched,
                        status="CANCELLED",
                        outcome=token_info.get("outcome", ""),
                        event_id=token_info.get("event_id", ""),
                        timestamp=int(time.time() * 1000),
                    )
                self._orders_update_callback({
                    "type": "order",
                    "order": cb_order,
                    "order_type": order_type,
                    "orders": list(self._current_orders.values()),
                    "positions": list(self._positions.values()) if self._positions else [],
                })

        except Exception as e:
            self._log.error(f"Error processing order update: {e}")

    async def _process_trade_update(self, trade_data: dict) -> None:
        """
        处理成交更新（扁平格式）

        Trade 消息字段：
        - id: trade ID
        - status: MATCHED → MINED → CONFIRMED（终态）/ RETRYING → FAILED（终态）
        - maker_orders: [{order_id, matched_amount, ...}]
        - taker_order_id: taker 的 order_id
        - size: 成交数量
        - price: 成交价格

        CONFIRMED 是完全成交的终态信号。

        Args:
            trade_data: 成交数据（扁平结构）
        """
        try:
            trade_id = trade_data.get("id", "")
            status = trade_data.get("status", "")
            maker_orders = trade_data.get("maker_orders", [])
            taker_order_id = trade_data.get("taker_order_id", "")

            self._log.info(
                f"Trade update: id={trade_id}, status={status}, "
                f"maker_orders={len(maker_orders)}, taker={taker_order_id[:20] if taker_order_id else ''}"
            )

            # 只处理 CONFIRMED（完全成交终态）
            if status == "CONFIRMED" and trade_id in self._confirmed_trade_ids:
                self._log.debug(f"Trade CONFIRMED already processed, skip: id={trade_id}")
                return

            if status != "CONFIRMED":
                # 非 CONFIRMED 仍触发回调让 tracker 知道状态变化
                if self._orders_update_callback:
                    self._orders_update_callback({
                        "type": "trade",
                        "trade": {
                            "id": trade_id,
                            "status": status,
                            "maker_orders": maker_orders,
                            "taker_order_id": taker_order_id,
                            "size": trade_data.get("size", "0"),
                            "price": trade_data.get("price", "0"),
                        },
                        "positions": list(self._positions.values()) if self._positions else [],
                    })
                return

            # === CONFIRMED 处理 ===
            self._confirmed_trade_ids.add(trade_id)

            # 收集本次成交涉及的 order_id
            confirmed_order_ids: set[str] = set()
            for mo in maker_orders:
                oid = mo.get("order_id", "")
                if oid:
                    confirmed_order_ids.add(oid)
            if taker_order_id:
                confirmed_order_ids.add(taker_order_id)

            # 从活跃订单中找到匹配的订单，更新持仓，删除活跃订单
            for oid in confirmed_order_ids:
                order = self._current_orders.pop(oid, None)
                if not order:
                    continue

                self._log.info(
                    f"Trade CONFIRMED: removing order {oid[:20]}..., "
                    f"updating position for asset={order.asset_id[:20]}..."
                )

                # 更新持仓（不调 fetch_positions）
                self._update_position_from_order(order)

            # 触发回调（通知 tracker）
            if self._orders_update_callback:
                self._orders_update_callback({
                    "type": "trade",
                    "trade": {
                        "id": trade_id,
                        "status": status,
                        "maker_orders": maker_orders,
                        "taker_order_id": taker_order_id,
                        "size": trade_data.get("size", "0"),
                        "price": trade_data.get("price", "0"),
                    },
                    "positions": list(self._positions.values()) if self._positions else [],
                })

        except Exception as e:
            self._log.error(f"Error processing trade update: {e}")

    def _update_position_from_order(self, order: PolymarketOrder) -> None:
        """
        根据已成交订单更新内存持仓

        BUY → 持仓增加 original_size
        SELL → 持仓减少 original_size

        Args:
            order: 已成交的订单
        """
        if self._positions is None:
            self._positions = {}

        asset_id = order.asset_id
        filled_size = order.original_size
        token_info = self._subscribed_tokens.get(asset_id, {})

        existing = self._positions.get(asset_id)

        if order.side == "BUY":
            if existing:
                # 加权平均价格
                total_size = existing.size + filled_size
                if total_size > 0:
                    existing.avg_price = (
                        existing.avg_price * existing.size + order.price * filled_size
                    ) / total_size
                existing.size = total_size
            else:
                self._positions[asset_id] = PolymarketPosition(
                    asset_id=asset_id,
                    condition_id=order.market,
                    outcome=order.outcome or token_info.get("outcome", ""),
                    market_type=token_info.get("market_type", ""),
                    size=filled_size,
                    avg_price=order.price,
                    current_price=order.price,
                    event_id=order.event_id or token_info.get("event_id", ""),
                    neg_risk=token_info.get("neg_risk", False),
                )
        elif order.side == "SELL":
            if existing:
                existing.size = max(0, existing.size - filled_size)
                # size 为 0 则移除
                if existing.size <= 0:
                    self._positions.pop(asset_id, None)

        self._log.debug(
            f"Position updated: asset={asset_id[:20]}..., "
            f"side={order.side}, filled={filled_size}, "
            f"new_size={self._positions[asset_id].size if asset_id in self._positions else 0}"
        )

    def get_current_orders(
        self,
        asset_id: str | None = None,
        condition_id: str | None = None,
    ) -> list[PolymarketOrder]:
        """
        获取当前订单

        Args:
            asset_id: 可选，指定 asset_id (token_id)
            condition_id: 可选，指定 condition_id (market)

        Returns:
            订单列表
        """
        if condition_id:
            return [o for o in self._current_orders.values() if o.market == condition_id]
        if asset_id:
            return [o for o in self._current_orders.values() if o.asset_id == asset_id]
        return list(self._current_orders.values())

    def get_positions(self, event_id: str | None = None) -> list[PolymarketPosition]:
        """
        获取持仓

        Args:
            event_id: 可选，指定 event_id

        Returns:
            持仓列表
        """
        if event_id:
            return [p for p in self._positions.values() if p.event_id == event_id]
        if not self._positions:
            return []
        return list(self._positions.values())

    def get_positions_by_pair(self, pair_id: str) -> list[PolymarketPosition]:
        """
        获取指定比赛的持仓（通过 event_id 匹配）

        Args:
            pair_id: 比赛 ID（通常等于 event_id）

        Returns:
            持仓列表
        """
        # pair_id 通常与 event_id 相同或有映射关系
        if not self._positions:
            return []
        return [p for p in self._positions.values() if p.event_id == pair_id]

    async def wait_for_positions(self, timeout: float) -> bool:
        """等待首次 positions 到达"""
        if self._positions is not None:
            return True
        try:
            await asyncio.wait_for(self._positions_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def register_orders_callback(self, callback: Callable[[dict], None]) -> None:
        """注册订单更新回调"""
        self._orders_update_callback = callback

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
            new_count = 0
            for token_info in tokens:
                token_id = token_info["token_id"]
                if token_id not in self._subscribed_tokens:
                    new_count += 1
                self._subscribed_tokens[token_id] = token_info
                token_ids.append(token_id)

            self._log.info(
                f"Total subscribed tokens: {len(self._subscribed_tokens)} "
                f"(new: {new_count}, resubscribe: {len(token_ids) - new_count})"
            )

            # 如果是重新订阅（解决 stale 问题），先重连 WebSocket
            if new_count == 0 and len(token_ids) > 0:
                self._log.info("Resubscribing to resolve stale data - reconnecting WebSocket")
                # 关闭现有连接
                if self._ws:
                    try:
                        await self._ws.close()
                    except Exception as e:
                        self._log.warning(f"Error closing WebSocket: {e}")
                    self._ws = None

                # 重连会在 _run_websocket 循环中自动完成
                # 新订阅会在连接成功后发送
                self._pending_subscribe.extend(token_ids)
                return

            # 首次订阅：直接发送
            await self._send_subscribe(token_ids)

            # 启动 WebSocket（如果还未启动）
            if not self._ws_task or self._ws_task.done():
                self._log.info("Starting WebSocket task")
                self._ws_task = asyncio.create_task(self._run_websocket())

            # User Channel 已连接时，重新发送订阅消息以包含新增的 condition_id
            if self._user_ws:
                await self._subscribe_user_channel()

        self._log.info(f"Subscribed to {len(tokens)} tokens for event {event_id}")

    # =========================================================================
    # 回调管理
    # =========================================================================

    def on_price_update(self, callback: Callable[[dict], None]) -> None:
        """设置价格更新回调"""
        self._price_update_callback = callback

    def register_positions_callback(self, callback: Callable[[str], None]) -> None:
        """注册仓位更新回调（参数为 event_id）"""
        self._positions_update_callback = callback

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
        self._current_orders.clear()
        self._positions.clear()
        self._confirmed_trade_ids.clear()
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

        # 启动 User Channel WebSocket（如果配置了 API key）
        if self.config.polymarket_clob_api_key:
            self._log.info("Starting User Channel WebSocket for order tracking")
            self._user_ws_task = asyncio.create_task(self._run_user_websocket())
        else:
            self._log.info("No API key configured, User Channel disabled")

    async def stop(self) -> None:
        """停止客户端"""
        self._running = False

        # 停止 Market Channel
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.close()
            self._ws = None

        # 停止 User Channel
        if self._user_ws_task and not self._user_ws_task.done():
            self._user_ws_task.cancel()
            try:
                await self._user_ws_task
            except asyncio.CancelledError:
                pass

        if self._user_ws:
            await self._user_ws.close()
            self._user_ws = None

        self._log.info("Polymarket odds client stopped")
