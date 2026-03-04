"""
诊断 Polymarket User Channel WebSocket 连接问题

测试两个端点：
1. CLOB WS: wss://ws-subscriptions-clob.polymarket.com/ws/user
2. Live Data WS: wss://ws-live-data.polymarket.com
"""

import asyncio
import json
import os
import sys

import websockets
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("POLYMARKET_API_KEY", "")
API_SECRET = os.getenv("POLYMARKET_API_SECRET", "")
PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "")

# 从日志中取的 condition_id
CONDITION_ID = "0x08fb9628d29d0ed8cad9b7f76395783e04a7e57547e2bdc83bba06ff4d5eb4ca"


async def test_clob_ws():
    """测试旧版 CLOB WebSocket 端点"""
    url = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    print(f"\n=== Test 1: CLOB WS ({url}) ===")
    print(f"apiKey: {API_KEY[:12]}...")
    print(f"secret: {'set' if API_SECRET else 'EMPTY'}")
    print(f"passphrase: {'set' if PASSPHRASE else 'EMPTY'}")

    try:
        ws = await websockets.connect(url, ping_interval=10, ping_timeout=5)
        print("Connected!")

        # 发送订阅
        msg = {
            "auth": {
                "apiKey": API_KEY,
                "secret": API_SECRET,
                "passphrase": PASSPHRASE,
            },
            "markets": [CONDITION_ID],
            "type": "user",
        }
        await ws.send(json.dumps(msg))
        print(f"Sent subscription: {json.dumps(msg)[:200]}...")

        # 等待消息
        try:
            response = await asyncio.wait_for(ws.recv(), timeout=10)
            print(f"Received: {response[:500]}")
        except asyncio.TimeoutError:
            print("No message received in 10 seconds (connection still alive)")
        except websockets.exceptions.ConnectionClosed as e:
            print(f"Connection closed: code={e.code}, reason='{e.reason}'")

        await ws.close()
    except Exception as e:
        print(f"Error: {e}")


async def test_live_data_ws():
    """测试新版 Live Data WebSocket 端点"""
    url = "wss://ws-live-data.polymarket.com"
    print(f"\n=== Test 2: Live Data WS ({url}) ===")

    try:
        ws = await websockets.connect(url, ping_interval=10, ping_timeout=5)
        print("Connected!")

        # 新版订阅格式 (参考 real-time-data-client)
        msg = {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "clob_user",
                    "type": "*",
                    "clob_auth": {
                        "key": API_KEY,
                        "secret": API_SECRET,
                        "passphrase": PASSPHRASE,
                    },
                }
            ],
        }
        await ws.send(json.dumps(msg))
        print(f"Sent subscription: {json.dumps(msg)[:200]}...")

        # 等待消息
        for i in range(3):
            try:
                response = await asyncio.wait_for(ws.recv(), timeout=10)
                print(f"Received [{i}]: {response[:500]}")
            except asyncio.TimeoutError:
                print(f"No message [{i}] in 10 seconds (connection alive)")
                break
            except websockets.exceptions.ConnectionClosed as e:
                print(f"Connection closed: code={e.code}, reason='{e.reason}'")
                break

        await ws.close()
    except Exception as e:
        print(f"Error: {e}")


async def test_clob_ws_no_auth():
    """测试不带认证的 CLOB WS（看是否是连接本身的问题）"""
    url = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    print(f"\n=== Test 3: CLOB WS without auth ===")

    try:
        ws = await websockets.connect(url, ping_interval=10, ping_timeout=5)
        print("Connected (no auth)!")

        # 等待看服务端是否主动发消息或断开
        try:
            response = await asyncio.wait_for(ws.recv(), timeout=10)
            print(f"Received: {response[:500]}")
        except asyncio.TimeoutError:
            print("No message in 10 seconds (connection still alive without auth)")
        except websockets.exceptions.ConnectionClosed as e:
            print(f"Connection closed: code={e.code}, reason='{e.reason}'")

        await ws.close()
    except Exception as e:
        print(f"Error: {e}")


async def main():
    if not API_KEY:
        print("ERROR: POLYMARKET_API_KEY not set in .env")
        sys.exit(1)

    await test_clob_ws_no_auth()
    await test_clob_ws()
    await test_live_data_ws()


if __name__ == "__main__":
    asyncio.run(main())
