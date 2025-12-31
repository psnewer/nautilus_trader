"""
分析 OrbitExch 页面结构

帮助找到正确的选择器
"""

import asyncio
from pathlib import Path
from nautilus_trader.adapters.orbitexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.orbitexch.scraper import OrbitExchScraper
from nautilus_trader.adapters.orbitexch.config_loader import load_config


async def analyze_page_structure():
    """分析页面结构"""
    
    print('分析 OrbitExch 页面结构')
    print('=' * 70)
    
    config = load_config(env='dev')
    manager = PlaywrightBrowserManager(headless=False)
    
    try:
        await manager.start()
        page = await manager.create_page('main')
        scraper = OrbitExchScraper(page)
        
        # 登录
        await scraper.login(config['username'], config['password'])
        
        # 导航到 in-play 页面
        await page.goto(f'{scraper.base_url}/customer/inplay/highlights')
        await asyncio.sleep(3)
        
        # 分析页面元素
        analysis = await page.evaluate('''() => {
            const result = {
                total_elements: document.querySelectorAll('*').length,
                buttons: [],
                links: [],
                inputs: [],
                divs_with_data_attrs: [],
            };
            
            // 分析按钮
            document.querySelectorAll('button').forEach((btn, i) => {
                if (i < 20) {  // 只分析前 20 个
                    result.buttons.push({
                        text: btn.textContent?.trim().substring(0, 30),
                        classes: btn.className,
                        visible: btn.offsetParent !== null,
                    });
                }
            });
            
            // 分析有 data- 属性的 div
            document.querySelectorAll('[data-event-id], [data-market-id], [data-selection-id]').forEach((el, i) => {
                if (i < 10) {
                    result.divs_with_data_attrs.push({
                        tag: el.tagName,
                        event_id: el.getAttribute('data-event-id'),
                        market_id: el.getAttribute('data-market-id'),
                        selection_id: el.getAttribute('data-selection-id'),
                        classes: el.className,
                    });
                }
            });
            
            return result;
        }''')
        
        print(f'总元素数: {analysis["total_elements"]}')
        print()
        
        print('按钮分析 (前 20 个):')
        for i, btn in enumerate(analysis['buttons'], 1):
            if btn['visible']:
                text = btn.get('text', '')
                classes = btn.get('classes', '')[:50]
                print(f'  {i}. "{text}" | Class: {classes}')
        
        print()
        print('带 data- 属性的元素:')
        for el in analysis['divs_with_data_attrs']:
            tag = el.get('tag', 'unknown')
            event_id = el.get('event_id', 'N/A')
            market_id = el.get('market_id', 'N/A')
            selection_id = el.get('selection_id', 'N/A')
            print(f'  {tag}: event={event_id}, market={market_id}, selection={selection_id}')
        
        # 保持浏览器打开
        print()
        print('=' * 70)
        print('浏览器保持打开 60 秒')
        print('按 F12 打开开发者工具，手动检查页面结构')
        print('=' * 70)
        
        await asyncio.sleep(60)
        
    finally:
        await manager.close()


if __name__ == '__main__':
    asyncio.run(analyze_page_structure())
