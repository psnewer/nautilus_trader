#!/usr/bin/env python3
"""
Polymarket WebSocket 测试脚本

测试与 Polymarket WebSocket 的连接和数据接收。

Usage:
    python scripts/test_polymarket_ws.py
"""

import asyncio
import json
import logging
import time

try:
    import websockets
except ImportError:
    print("Please install websockets: pip install websockets")
    exit(1)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger("PolymarketWS")

# Polymarket API 凭证
API_KEY = "019c11d1-873a-788d-a4e7-2065a2f9f36f"
API_SECRET = "1wF4nCed5DMLqbvl77jYmdmicZSQGxH0wP9tT3Zx9A8="
PASSPHRASE = "517b4b076ce1dde35d9ab989ee6e34b9b08d8714e37e760bbdaa0ad4fc96af70"

# WebSocket URLs
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS_URL = "wss://ws-live-data.polymarket.com"

# 测试用的 token IDs (来自 Leeds United FC vs. Arsenal FC 比赛 - event 170912)
TEST_TOKEN_IDS = [
    "18723404552010345747546699251640733729265713585558341088797021465644980538132",  # Leeds United FC win
    "79938577476899164135032019039357851709109332781933520320365603626744217425974",  # Arsenal FC win
    "91396479129193248223041776137293740265480202551113162045887108977455227597949",  # Draw
]

# 测试用的 condition ID (market ID)
TEST_CONDITION_ID = "0x1234567890abcdef"  # 需要替换为实际的 condition ID


async def test_rtds_channel():
    """测试 RTDS Channel（实时数据流，不需要认证）"""
    log.info(f"Connecting to RTDS: {RTDS_WS_URL}")

    try:
        async with websockets.connect(
            RTDS_WS_URL,
            ping_interval=30,
            ping_timeout=10,
            additional_headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            }
        ) as ws:
            log.info("Connected to RTDS WebSocket")

            # 订阅活动数据
            subscribe_msg = {
                "subscriptions": [
                    {"topic": "activity", "type": "trades"},
                    {"topic": "crypto_prices", "type": "update"},
                ]
            }

            await ws.send(json.dumps(subscribe_msg))
            log.info(f"Sent subscription: {subscribe_msg}")

            # 接收消息
            message_count = 0
            start_time = time.time()

            while message_count < 30 and (time.time() - start_time) < 60:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=10)
                    message_count += 1

                    try:
                        data = json.loads(message)
                        topic = data.get("topic", "")
                        msg_type = data.get("type", "")
                        payload = data.get("payload", {})

                        log.info(f"[{message_count}] {topic}/{msg_type}: {json.dumps(payload)[:200]}")

                    except json.JSONDecodeError:
                        log.warning(f"[{message_count}] Non-JSON: {message[:100]}")

                except asyncio.TimeoutError:
                    log.info("No message in 10s")

            log.info(f"Received {message_count} messages in {time.time() - start_time:.1f}s")

    except Exception as e:
        log.error(f"RTDS error: {e}")
        import traceback
        traceback.print_exc()


async def test_clob_market_channel():
    """测试 CLOB Market Channel"""
    log.info(f"Connecting to CLOB Market: {CLOB_WS_URL}")

    # 尝试不同的连接参数
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Origin": "https://polymarket.com",
    }

    try:
        async with websockets.connect(
            CLOB_WS_URL,
            ping_interval=30,
            ping_timeout=10,
            additional_headers=headers,
            close_timeout=5,
        ) as ws:
            log.info("Connected to CLOB Market WebSocket")

            # 订阅 market 数据
            subscribe_msg = {
                "assets_ids": TEST_TOKEN_IDS,
                "type": "market",
            }

            await ws.send(json.dumps(subscribe_msg))
            log.info(f"Sent subscription for {len(TEST_TOKEN_IDS)} tokens")

            # 接收消息
            message_count = 0
            start_time = time.time()

            while message_count < 20 and (time.time() - start_time) < 60:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=10)
                    message_count += 1

                    try:
                        data = json.loads(message)
                        await process_clob_message(data, message_count)
                    except json.JSONDecodeError:
                        log.warning(f"[{message_count}] Non-JSON: {message[:100]}")

                except asyncio.TimeoutError:
                    log.info("Sending PING...")
                    await ws.send("PING")

            log.info(f"Received {message_count} messages")

    except websockets.exceptions.InvalidStatusCode as e:
        log.error(f"Connection rejected with status {e.status_code}")
    except ConnectionResetError as e:
        log.error(f"Connection reset: {e}")
    except Exception as e:
        log.error(f"CLOB error: {e}")
        import traceback
        traceback.print_exc()


async def process_clob_message(data, msg_num: int):
    """处理 CLOB 消息"""
    # 处理列表格式
    if isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, dict):
                await process_single_clob_message(item, f"{msg_num}.{i}")
        return

    if isinstance(data, dict):
        await process_single_clob_message(data, str(msg_num))


async def process_single_clob_message(data: dict, msg_id: str):
    """处理单个 CLOB 消息"""
    event_type = data.get("event_type", "unknown")
    asset_id = data.get("asset_id", "")

    if len(asset_id) > 20:
        asset_short = asset_id[:10] + "..." + asset_id[-5:]
    else:
        asset_short = asset_id

    if event_type == "book":
        bids = data.get("bids", []) or data.get("buys", [])
        asks = data.get("asks", []) or data.get("sells", [])

        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 0

        log.info(
            f"[{msg_id}] BOOK: {asset_short} "
            f"bid={best_bid:.4f} ask={best_ask:.4f} "
            f"depth: {len(bids)}/{len(asks)}"
        )

    elif event_type == "price_change":
        price = data.get("price", 0)
        side = data.get("side", "")
        best_bid = data.get("best_bid", 0)
        best_ask = data.get("best_ask", 0)

        log.info(
            f"[{msg_id}] PRICE: {asset_short} "
            f"{side}@{price} bid/ask={best_bid}/{best_ask}"
        )

    elif event_type == "last_trade_price":
        price = data.get("price", 0)
        side = data.get("side", "")
        size = data.get("size", 0)

        log.info(
            f"[{msg_id}] TRADE: {asset_short} "
            f"{side}@{price} size={size}"
        )

    else:
        log.info(f"[{msg_id}] {event_type}: {json.dumps(data)[:150]}")


async def test_clob_with_auth():
    """测试带认证的 CLOB 连接"""
    ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    log.info(f"Connecting to CLOB User (with auth): {ws_url}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Origin": "https://polymarket.com",
    }

    try:
        async with websockets.connect(
            ws_url,
            ping_interval=30,
            ping_timeout=10,
            additional_headers=headers,
        ) as ws:
            log.info("Connected")

            # 发送认证消息
            auth_msg = {
                "auth": {
                    "apiKey": API_KEY,
                    "secret": API_SECRET,
                    "passphrase": PASSPHRASE,
                },
                "type": "user",
            }

            await ws.send(json.dumps(auth_msg))
            log.info("Sent auth message")

            # 等待响应
            for i in range(5):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    log.info(f"Response [{i}]: {msg[:300]}")
                except asyncio.TimeoutError:
                    break

    except Exception as e:
        log.error(f"Auth error: {e}")
        import traceback
        traceback.print_exc()


async def main():
    """主函数"""
    log.info("=" * 60)
    log.info("Polymarket WebSocket Test")
    log.info("=" * 60)

    # 1. 测试 RTDS（实时数据流）
    log.info("\n--- Testing RTDS WebSocket ---\n")
    await test_rtds_channel()

    # 2. 测试 CLOB Market Channel
    log.info("\n--- Testing CLOB Market Channel ---\n")
    await test_clob_market_channel()

    # 3. 测试带认证的 CLOB
    log.info("\n--- Testing CLOB with Auth ---\n")
    await test_clob_with_auth()

    log.info("\n" + "=" * 60)
    log.info("Test completed")


if __name__ == "__main__":
    asyncio.run(main())
