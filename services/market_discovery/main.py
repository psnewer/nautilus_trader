"""
市场发现服务 - 主入口

启动所有平台适配器
"""

import asyncio
import logging
import signal
from pathlib import Path
import sys

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from services.market_discovery.service import MarketDiscoveryService
from services.market_discovery.orbitexch_adapter import OrbitExchAdapter
# from services.market_discovery.polymarket_adapter import PolymarketAdapter  # TODO
from dotenv import load_dotenv
import os


async def main():
    """主函数"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print('=' * 70)
    print('市场发现服务')
    print('=' * 70)
    print()
    
    # 加载配置
    load_dotenv(encoding='utf-8')
    
    # 创建服务
    service = MarketDiscoveryService(output_dir='./data/markets')
    
    # 注册适配器
    print('注册平台适配器...')
    
    # OrbitExch
    orbitexch_adapter = OrbitExchAdapter(
        username=os.getenv('ORBITEXCH_USERNAME'),
        password=os.getenv('ORBITEXCH_PASSWORD'),
    )
    service.register_adapter(orbitexch_adapter)
    
    # Polymarket
    # polymarket_adapter = PolymarketAdapter(...)  # TODO
    # service.register_adapter(polymarket_adapter)
    
    print()
    
    # 启动服务
    await service.start()
    
    # 处理 Ctrl+C
    def signal_handler(sig, frame):
        print('\n收到停止信号...')
        asyncio.create_task(service.stop())
    
    signal.signal(signal.SIGINT, signal_handler)
    
    # 保持运行
    try:
        while service._running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await service.stop()


if __name__ == '__main__':
    asyncio.run(main())
