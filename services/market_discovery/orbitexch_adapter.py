"""
OrbitExch 平台适配器

实现 PlatformAdapter 接口
"""

import asyncio
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from services.market_discovery.service import PlatformAdapter, StandardMarket
from nautilus_trader.adapters.orbitexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.orbitexch.scraper import OrbitExchScraper
from nautilus_trader.adapters.orbitexch.websocket_handler import OrbitExchWebSocketHandler
from nautilus_trader.adapters.orbitexch.message_parser import OrbitExchMessageParser
from typing import List
from datetime import datetime
from dotenv import load_dotenv
import os


class OrbitExchAdapter(PlatformAdapter):
    """OrbitExch 平台适配器"""
    
    def __init__(self, username: str, password: str):
        super().__init__('ORBITEXCH')
        self.username = username
        self.password = password
        
        self.browser_manager = None
        self.page = None
        self.scraper = None
        self.ws_handler = None
        self.parser = OrbitExchMessageParser()
        
        # 缓存市场数据
        self.markets_cache = {}
    
    async def start(self):
        """启动适配器 - 连接到 OrbitExch"""
        self._log.info('启动 OrbitExch 适配器...')
        
        # 启动浏览器
        self.browser_manager = PlaywrightBrowserManager(
            browser_type='chromium',
            headless=False,  # 改为 True 用于生产环境
        )
        await self.browser_manager.start()
        
        # 登录
        self.page = await self.browser_manager.create_page('discovery')
        self.scraper = OrbitExchScraper(self.page)
        
        success = await self.scraper.login(self.username, self.password)
        if not success:
            raise RuntimeError('OrbitExch 登录失败')
        
        # 导航到市场页面
        await self.page.goto('https://orbitexch.com/customer/inplay/highlights')
        await asyncio.sleep(2)
        
        # 启动 WebSocket 监听
        self.ws_handler = OrbitExchWebSocketHandler(self.page)
        self.ws_handler.on_price_update(self._on_price_update)
        await self.ws_handler.start()
        
        self._log.info('✅ OrbitExch 适配器已启动')
    
    async def stop(self):
        """停止适配器"""
        self._log.info('停止 OrbitExch 适配器...')
        
        if self.ws_handler:
            await self.ws_handler.stop()
        
        if self.browser_manager:
            await self.browser_manager.close()
        
        self._log.info('✅ OrbitExch 适配器已停止')
    
    def _on_price_update(self, message: dict):
        """WebSocket 价格更新回调"""
        parsed = self.parser.parse_price_message(message)
        if not parsed:
            return
        
        # 更新缓存
        market_id = parsed['market_id']
        self.markets_cache[market_id] = parsed
    
    async def discover_markets(self) -> List[StandardMarket]:
        """发现市场"""
        markets = []
        
        # 从缓存转换为标准格式
        for market_id, data in self.markets_cache.items():
            try:
                standard_market = self._convert_to_standard(data)
                markets.append(standard_market)
            except Exception as e:
                self._log.error(f'转换市场失败 {market_id}: {e}')
        
        return markets
    
    def _convert_to_standard(self, data: dict) -> StandardMarket:
        """将 OrbitExch 数据转换为标准格式"""
        
        # 转换 outcomes
        outcomes = []
        for runner in data.get('runners', []):
            outcomes.append({
                'id': runner['selection_id'],
                'name': f'Selection {runner["selection_id"]}',  # TODO: 获取真实名称
                'back_price': runner['back'][0]['price'] if runner['back'] else None,
                'lay_price': runner['lay'][0]['price'] if runner['lay'] else None,
                'back_size': runner['back'][0]['size'] if runner['back'] else 0,
                'lay_size': runner['lay'][0]['size'] if runner['lay'] else 0,
            })
        
        return StandardMarket(
            platform='ORBITEXCH',
            market_id=data['market_id'],
            event_id=data['event_id'],
            event_name=data['event_name'],
            market_type=data['market_name'],
            sport='UNKNOWN',  # TODO: 从 event_name 推断
            start_time=datetime.now().isoformat(),  # TODO: 解析真实时间
            discovered_at=datetime.now().isoformat(),
            status=data['status'],
            in_play=data['in_play'],
            outcomes=outcomes,
            metadata={
                'timestamp': data.get('timestamp'),
            }
        )


# 测试运行
async def test_adapter():
    """测试适配器"""
    import logging
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    load_dotenv(encoding='utf-8')
    username = os.getenv('ORBITEXCH_USERNAME')
    password = os.getenv('ORBITEXCH_PASSWORD')
    
    adapter = OrbitExchAdapter(username, password)
    
    try:
        await adapter.start()
        
        print('等待 30 秒收集数据...')
        await asyncio.sleep(30)
        
        markets = await adapter.discover_markets()
        print(f'发现 {len(markets)} 个市场')
        
        for market in markets[:5]:
            print(f'  - {market.event_name} ({market.market_type})')
        
    finally:
        await adapter.stop()


if __name__ == '__main__':
    asyncio.run(test_adapter())
