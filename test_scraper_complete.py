"""
OrbitExch Scraper 完整功能测试

测试所有 scraper 功能
"""

import asyncio
import logging
from pathlib import Path

from nautilus_trader.adapters.orbitexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.orbitexch.scraper import OrbitExchScraper
from nautilus_trader.adapters.orbitexch.config_loader import load_config


async def test_all_scraper_functions():
    """测试所有 scraper 功能"""
    
    # 设置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print('=' * 70)
    print('OrbitExch Scraper 完整功能测试')
    print('=' * 70)
    print()
    
    # 加载配置
    try:
        config = load_config(env='dev')
        username = config['username']
        password = config['password']
    except Exception as e:
        print(f'❌ 配置加载失败: {e}')
        return
    
    # 创建浏览器管理器
    manager = PlaywrightBrowserManager(
        browser_type='chromium',
        headless=False,  # 显示浏览器便于观察
    )
    
    try:
        # ========== 1. 启动浏览器 ==========
        print('1️⃣  启动浏览器...')
        await manager.start()
        page = await manager.create_page('main')
        scraper = OrbitExchScraper(page)
        print('✅ 浏览器已启动')
        print()
        
        # ========== 2. 登录 ==========
        print('2️⃣  测试登录...')
        success = await scraper.login(username, password)
        
        if not success:
            print('❌ 登录失败')
            return
        
        print('✅ 登录成功（弹窗已处理）')
        await manager.screenshot('main', 'docs/screenshots/logged_in.png')
        print('   截图: docs/screenshots/logged_in.png')
        print()
        
        # ========== 3. 获取账户余额 ==========
        print('3️⃣  获取账户余额...')
        balance = await scraper.get_balance()
        print(f'   余额: {balance.get("balance", 0)}')
        print(f'   可用: {balance.get("available", 0)}')
        print(f'   风险: {balance.get("exposure", 0)}')
        print()
        
        # ========== 4. 抓取 In-Play 赛事 ==========
        print('4️⃣  抓取 In-Play 赛事...')
        events = await scraper.scrape_inplay_events()
        
        if events:
            print(f'   找到 {len(events)} 个赛事:')
            for i, event in enumerate(events[:10], 1):  # 只显示前 10 个
                event_name = event.get('event_name', 'Unknown')
                event_id = event.get('event_id', 'N/A')
                print(f'   {i}. {event_name} (ID: {event_id})')
        else:
            print('   ⚠️  未找到赛事（可能需要调整选择器）')
        
        await manager.screenshot('main', 'docs/screenshots/inplay_events.png')
        print('   截图: docs/screenshots/inplay_events.png')
        print()
        
        # ========== 5. 分析页面结构（帮助完善选择器）==========
        print('5️⃣  分析页面结构...')
        
        # 获取所有包含 "event" 或 "match" 的类名
        page_classes = await page.evaluate('''() => {
            const elements = document.querySelectorAll('*');
            const classes = new Set();
            
            elements.forEach(el => {
                if (el.className && typeof el.className === 'string') {
                    el.className.split(' ').forEach(cls => {
                        if (cls && (
                            cls.toLowerCase().includes('event') ||
                            cls.toLowerCase().includes('match') ||
                            cls.toLowerCase().includes('game') ||
                            cls.toLowerCase().includes('odd') ||
                            cls.toLowerCase().includes('bet')
                        )) {
                            classes.add(cls);
                        }
                    });
                }
            });
            
            return Array.from(classes);
        }''')
        
        print(f'   相关 CSS 类名 ({len(page_classes)} 个):')
        for cls in sorted(page_classes)[:15]:
            print(f'     • {cls}')
        
        # 保存页面 HTML 供分析
        html = await page.content()
        with open('docs/html/inplay_page.html', 'w', encoding='utf-8') as f:
            f.write(html)
        print('   HTML 保存: docs/html/inplay_page.html')
        print()
        
        # ========== 6. 手动检查时间 ==========
        print('=' * 70)
        print('浏览器将保持打开 60 秒')
        print('请手动检查页面，观察：')
        print('  - 赛事如何排列')
        print('  - 赔率如何展示')
        print('  - Back/Lay 按钮的样式')
        print('=' * 70)
        
        await asyncio.sleep(60)
        
    except Exception as e:
        print(f'❌测试失败: {e}')
        import traceback
        traceback.print_exc()
    
    finally:
        await manager.close()
        print()
        print('✅ 测试完成')


if __name__ == '__main__':
    # 确保目录存在
    Path('docs/screenshots').mkdir(parents=True, exist_ok=True)
    Path('docs/html').mkdir(parents=True, exist_ok=True)
    
    asyncio.run(test_all_scraper_functions())
