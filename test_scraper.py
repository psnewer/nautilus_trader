"""
测试 OrbitExch Scraper

手动测试脚本，需要真实账户
"""

import asyncio
import logging
from pathlib import Path

from nautilus_trader.adapters.orbitexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.orbitexch.scraper import OrbitExchScraper
from nautilus_trader.adapters.orbitexch.config_loader import load_config


async def test_scraper():
    """Test OrbitExch scraper functionality"""
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print('=' * 70)
    print('OrbitExch Scraper 测试')
    print('=' * 70)
    print()
    
    # Load config
    try:
        config = load_config(env='dev')
        username = config.get('username')
        password = config.get('password')
        
        if not username or not password:
            print('❌ 请在 .env 文件中设置 ORBITEXCH_USERNAME 和 ORBITEXCH_PASSWORD')
            return
    except Exception as e:
        print(f'❌ 配置加载失败: {e}')
        print('提示: 复制 .env.example 为 .env 并填入账户信息')
        return
    
    # Create browser manager
    manager = PlaywrightBrowserManager(
        browser_type='chromium',
        headless=False,  # 显示浏览器
    )
    
    try:
        # Start browser
        print('1️⃣  启动浏览器...')
        await manager.start()
        
        # Create page
        page = await manager.create_page('main')
        
        # Create scraper
        scraper = OrbitExchScraper(page)
        
        # Test login
        print()
        print('2️⃣  测试登录...')
        success = await scraper.login(username, password)
        
        if not success:
            print('❌ 登录失败，请检查账户信息')
            return
        
        print('✅ 登录成功！')
        
        # Screenshot after login
        await manager.screenshot('main', 'docs/screenshots/after_login.png')
        print('   截图保存: docs/screenshots/after_login.png')
        
        # Test scraping in-play events
        print()
        print('3️⃣  抓取 In-Play 赛事...')
        events = await scraper.scrape_inplay_events()
        
        print(f'   找到 {len(events)} 个赛事:')
        for event in events[:5]:
            print(f'     • {event["event_name"]} (ID: {event["event_id"]})')
        
        # Test getting balance
        print()
        print('4️⃣  获取账户余额...')
        balance = await scraper.get_balance()
        print(f'   余额: {balance["balance"]}')
        print(f'   可用: {balance["available"]}')
        
        # Keep browser open for manual inspection
        print()
        print('=' * 70)
        print('浏览器将保持打开 30 秒，请手动检查')
        print('=' * 70)
        await asyncio.sleep(30)
        
    except Exception as e:
        print(f'❌ 测试失败: {e}')
        import traceback
        traceback.print_exc()
    
    finally:
        await manager.close()
        print()
        print('✅ 测试完成')


if __name__ == '__main__':
    # Create screenshots directory
    Path('docs/screenshots').mkdir(parents=True, exist_ok=True)
    
    asyncio.run(test_scraper())
