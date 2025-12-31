"""
最简化的 DataClient 测试 - 直接创建配置
"""

import asyncio
import logging

from nautilus_trader.adapters.orbitexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.orbitexch.config import OrbitExchDataClientConfig
from nautilus_trader.adapters.orbitexch.data import OrbitExchDataClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from dotenv import load_dotenv
import os


async def test_data_client():
    """测试 DataClient"""
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print('=' * 70)
    print('OrbitExch DataClient 测试')
    print('=' * 70)
    print()
    
    # 加载环境变量
    load_dotenv(encoding='utf-8')
    
    # 直接创建配置
    config = OrbitExchDataClientConfig(
        username=os.getenv('ORBITEXCH_USERNAME', 'haidi123'),
        password=os.getenv('ORBITEXCH_PASSWORD', '594110_Aa'),
        base_url='https://orbitexch.com',
        headless=False,
        browser_type='chromium',
        user_data_dir='./browser_data/orbitexch_dev',
        page_timeout=30000,
        scrape_interval_ms=1000,
    )
    
    # 创建依赖
    loop = asyncio.get_event_loop()
    msgbus = MessageBus()
    cache = Cache()
    clock = LiveClock()
    
    # 创建 browser manager
    browser_manager = PlaywrightBrowserManager(
        browser_type=config.browser_type,
        headless=config.headless,
    )
    
    # 创建 DataClient
    data_client = OrbitExchDataClient(
        loop=loop,
        client=browser_manager,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        config=config,
    )
    
    try:
        # 连接
        print('1️⃣  连接...')
        await data_client._connect()
        print()
        
        # 订阅市场
        print('2️⃣  订阅市场...')
        await data_client.subscribe_market(
            market_id='1.252123015',
            selection_id='61640820'
        )
        print()
        
        # 等待数据
        print('3️⃣  等待数据 (30 秒)...')
        print('观察日志中的价格更新')
        print('=' * 70)
        await asyncio.sleep(30)
        
        print()
        print('✅ 测试完成')
        
    except Exception as e:
        print(f'❌ 错误: {e}')
        import traceback
        traceback.print_exc()
    
    finally:
        await data_client._disconnect()


if __name__ == '__main__':
    asyncio.run(test_data_client())
