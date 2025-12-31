"""
测试 OrbitExch WebSocket 拦截

监听并解析实时赔率和订单数据
"""

import asyncio
import logging
import json
from pathlib import Path

from nautilus_trader.adapters.orbitexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.orbitexch.scraper import OrbitExchScraper
from nautilus_trader.adapters.orbitexch.websocket_handler import OrbitExchWebSocketHandler
from nautilus_trader.adapters.orbitexch.config_loader import load_config


# 记录收到的消息
price_messages = []
order_messages = []


def on_price_update(message):
    """处理赔率更新"""
    print(f'📊 赔率更新: {str(message)[:150]}...')
    price_messages.append(message)
    
    # 保存前 10 条消息供分析
    if len(price_messages) <= 10:
        with open('docs/websocket_price_samples.json', 'w', encoding='utf-8') as f:
            json.dump(price_messages, f, indent=2, ensure_ascii=False)


def on_order_update(message):
    """处理订单更新"""
    print(f'📋 订单更新: {str(message)[:150]}...')
    order_messages.append(message)
    
    # 保存前 10 条消息供分析
    if len(order_messages) <= 10:
        with open('docs/websocket_order_samples.json', 'w', encoding='utf-8') as f:
            json.dump(order_messages, f, indent=2, ensure_ascii=False)


async def test_websocket():
    """测试 WebSocket 拦截"""
    
    # 设置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print('=' * 70)
    print('OrbitExch WebSocket 测试')
    print('=' * 70)
    print()
    
    # 加载配置
    config = load_config(env='dev')
    
    # 创建浏览器管理器
    manager = PlaywrightBrowserManager(
        browser_type='chromium',
        headless=False,
    )
    
    try:
        # 启动浏览器
        print('1️⃣  启动浏览器...')
        await manager.start()
        page = await manager.create_page('main')
        
        # 创建 WebSocket 处理器
        print('2️⃣  初始化 WebSocket 处理器...')
        ws_handler = OrbitExchWebSocketHandler(page)
        
        # 注册回调
        ws_handler.on_price_update(on_price_update)
        ws_handler.on_order_update(on_order_update)
        
        # 启动监听
        await ws_handler.start()
        
        # 创建 scraper 并登录
        print('3️⃣  登录...')
        scraper = OrbitExchScraper(page)
        success = await scraper.login(config['username'], config['password'])
        
        if not success:
            print('❌ 登录失败')
            return
        
        print('✅ 登录成功')
        print()
        
        # 导航到 in-play 页面（触发 WebSocket 连接）
        print('4️⃣  导航到 In-Play 页面...')
        await page.goto(f'{scraper.base_url}/customer/inplay/highlights')
        await asyncio.sleep(3)
        
        # 显示活跃的 WebSocket
        active_ws = ws_handler.get_active_websockets()
        print(f'✅ 活跃的 WebSocket: {len(active_ws)} 个')
        for ws in active_ws:
            ws_type = ws.get('type', 'unknown')
            ws_url = ws.get('url', 'N/A')
            print(f'   - {ws_type}: {ws_url}')
        print()
        
        # 监听消息
        print('5️⃣  监听 WebSocket 消息...')
        print('=' * 70)
        print('保持监听 60 秒')
        print('请在浏览器中点击不同的赛事，观察数据流')
        print('=' * 70)
        print()
        
        await asyncio.sleep(60)
        
        # 停止监听
        await ws_handler.stop()
        
        # 显示统计
        print()
        print('=' * 70)
        print('统计信息')
        print('=' * 70)
        print(f'赔率消息: {len(price_messages)} 条')
        print(f'订单消息: {len(order_messages)} 条')
        print()
        
        if price_messages:
            print('✅ 赔率消息样本已保存: docs/websocket_price_samples.json')
        
        if order_messages:
            print('✅ 订单消息样本已保存: docs/websocket_order_samples.json')
        
    except Exception as e:
        print(f'❌ 测试失败: {e}')
        import traceback
        traceback.print_exc()
    
    finally:
        await manager.close()
        print()
        print('✅ 测试完成')


if __name__ == '__main__':
    # 确保目录存在
    Path('docs').mkdir(exist_ok=True)
    
    asyncio.run(test_websocket())
