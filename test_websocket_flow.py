"""
独立的 WebSocket 数据流测试 - 不依赖 NautilusTrader MessageBus
"""

import asyncio
import logging
from collections import defaultdict

from nautilus_trader.adapters.orbitexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.orbitexch.scraper import OrbitExchScraper
from nautilus_trader.adapters.orbitexch.websocket_handler import OrbitExchWebSocketHandler
from nautilus_trader.adapters.orbitexch.message_parser import OrbitExchMessageParser
from dotenv import load_dotenv
import os


class SimpleDataCollector:
    """简单的数据收集器 - 用于测试"""
    
    def __init__(self):
        self.parser = OrbitExchMessageParser()
        self.price_updates = defaultdict(list)
        self.total_updates = 0
        self._log = logging.getLogger('SimpleDataCollector')
    
    def on_price_update(self, message: dict):
        """处理价格更新"""
        parsed = self.parser.parse_price_message(message)
        if not parsed:
            return
        
        market_id = parsed['market_id']
        event_name = parsed['event_name']
        
        self.total_updates += 1
        self.price_updates[market_id].append(parsed)
        
        # 只显示前几条，避免刷屏
        if self.total_updates <= 10:
            self._log.info(f'📊 [{self.total_updates}] {event_name}')
            for runner in parsed['runners'][:1]:  # 只显示第一个选手
                best_back = self.parser.get_best_back_price(runner)
                best_lay = self.parser.get_best_lay_price(runner)
                self._log.info(f'   Selection {runner["selection_id"]}: Back={best_back} Lay={best_lay}')
    
    def on_order_update(self, message: dict):
        """处理订单更新"""
        pass
    
    def print_summary(self):
        """打印统计摘要"""
        print()
        print('=' * 70)
        print('数据收集摘要')
        print('=' * 70)
        print(f'总更新数: {self.total_updates}')
        print(f'不同市场: {len(self.price_updates)}')
        for market_id, updates in list(self.price_updates.items())[:5]:
            print(f'  Market {market_id}: {len(updates)} 条更新')


async def test_websocket_data_flow():
    """测试 WebSocket 数据流"""
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    print('=' * 70)
    print('OrbitExch WebSocket 数据流测试')
    print('=' * 70)
    print()
    
    # 加载环境变量
    load_dotenv(encoding='utf-8')
    username = os.getenv('ORBITEXCH_USERNAME', 'haidi123')
    password = os.getenv('ORBITEXCH_PASSWORD', '594110_Aa')
    
    # 创建浏览器管理器
    manager = PlaywrightBrowserManager(
        browser_type='chromium',
        headless=False,
    )
    
    # 创建数据收集器
    collector = SimpleDataCollector()
    
    try:
        # 启动浏览器
        print('1️⃣  启动浏览器...')
        await manager.start()
        page = await manager.create_page('test')
        print('✅ 浏览器已启动')
        print()
        
        # 登录
        print('2️⃣  登录...')
        scraper = OrbitExchScraper(page)
        if not await scraper.login(username, password):
            print('❌ 登录失败')
            return
        print('✅ 登录成功')
        print()
        
        # 导航到 in-play 页面
        print('3️⃣  导航到 In-Play 页面...')
        await page.goto('https://orbitexch.com/customer/inplay/highlights')
        await asyncio.sleep(2)
        print('✅ 页面已加载')
        print()
        
        # 设置 WebSocket 监听
        print('4️⃣  启动 WebSocket 监听...')
        ws_handler = OrbitExchWebSocketHandler(page)
        ws_handler.on_price_update(collector.on_price_update)
        ws_handler.on_order_update(collector.on_order_update)
        await ws_handler.start()
        
        active_ws = ws_handler.get_active_websockets()
        print(f'✅ WebSocket 活跃: {len(active_ws)} 个')
        for ws in active_ws:
            print(f'   - {ws["type"]}')
        print()
        
        # 接收数据
        print('5️⃣  接收数据 (30 秒)...')
        print('=' * 70)
        await asyncio.sleep(30)
        
        # 停止监听
        await ws_handler.stop()
        
        # 显示摘要
        collector.print_summary()
        print()
        print('✅ 测试完成')
        
    except Exception as e:
        print(f'❌ 错误: {e}')
        import traceback
        traceback.print_exc()
    
    finally:
        await manager.close()


if __name__ == '__main__':
    asyncio.run(test_websocket_data_flow())
