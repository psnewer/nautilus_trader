"""
简单 WebSocket 监听 - 60 秒
"""

import asyncio
import logging
import json
from pathlib import Path
from nautilus_trader.adapters.orbitexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.orbitexch.scraper import OrbitExchScraper
from nautilus_trader.adapters.orbitexch.websocket_handler import OrbitExchWebSocketHandler
from nautilus_trader.adapters.orbitexch.config_loader import load_config

price_messages = []

def on_price(msg):
    print(f'📊 [{len(price_messages)}]')
    price_messages.append(msg)
    with open('docs/ws_prices.json', 'w', encoding='utf-8') as f:
        json.dump(price_messages[:50], f, indent=2)

async def main():
    logging.basicConfig(level=logging.INFO)
    config = load_config('dev')
    mgr = PlaywrightBrowserManager(headless=False)
    
    await mgr.start()
    page = await mgr.create_page('main')
    
    # WebSocket
    ws = OrbitExchWebSocketHandler(page)
    ws.on_price_update(on_price)
    await ws.start()
    
    # 登录
    scraper = OrbitExchScraper(page)
    await scraper.login(config['username'], config['password'])
    await page.goto(f'{scraper.base_url}/customer/inplay/highlights')
    
    print('✅ 开始监听 60 秒...')
    print('请手动滚动页面或点击赛事')
    
    await asyncio.sleep(60)
    
    await ws.stop()
    await mgr.close()
    
    print(f'✅ 收集到 {len(price_messages)} 条消息')
    print('保存到: docs/ws_prices.json')

if __name__ == '__main__':
    Path('docs').mkdir(exist_ok=True)
    asyncio.run(main())
