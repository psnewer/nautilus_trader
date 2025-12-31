"""
OrbitExch 菜单爬虫 - 先展开菜单
"""

import asyncio
import logging
from playwright.async_api import async_playwright
from dotenv import load_dotenv
import os
import json


async def test_menu_structure():
    """测试菜单结构"""
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    load_dotenv(encoding='utf-8')
    username = os.getenv('ORBITEXCH_USERNAME')
    password = os.getenv('ORBITEXCH_PASSWORD')
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        
        try:
            # 登录
            print('1️⃣  登录...')
            await page.goto('https://orbitexch.com/customer/login')
            await page.fill('input[name="username"]', username)
            await page.fill('input[name="password"]', password)
            await page.click('button[type="submit"]:has-text("Log In")')
            await asyncio.sleep(3)
            
            # 处理弹窗
            try:
                ok_button = page.locator('xpath=//button[normalize-space()="OK"]')
                if await ok_button.is_visible(timeout=3000):
                    await ok_button.click()
                    await asyncio.sleep(1)
            except:
                pass
            
            print('2️⃣  导航到页面...')
            await page.goto('https://orbitexch.com/customer/inplay/highlights')
            await asyncio.sleep(3)
            
            print()
            print('=' * 70)
            print('查找并展开 Sports 菜单')
            print('=' * 70)
            print()
            
            # 查找 Sports 菜单折叠按钮
            print('3️⃣  查找 Sports 折叠按钮...')
            
            # 根据 HTML，Sports 菜单的标题是一个 collapse 元素
            # <div id="a-sportsSection" class="... biab_sports-available-title">Sports</div>
            sports_section = page.locator('#a-sportsSection')
            
            if await sports_section.count() > 0:
                print('   找到 Sports 区域')
                
                # 检查是否已展开
                classes = await sports_section.get_attribute('class')
                print(f'   Class: {classes}')
                
                # 点击展开（如果未展开）
                if 'biab_opened' not in classes:
                    print('   点击展开 Sports...')
                    await sports_section.click()
                    await asyncio.sleep(2)
                else:
                    print('   Sports 已展开')
            else:
                print('   ❌ 未找到 #a-sportsSection')
            
            print()
            print('4️⃣  查找 Sport 节点...')
            
            # 等待 sport 列表加载
            await asyncio.sleep(2)
            
            # 现在查找 sport 节点
            sports = await page.locator('li[datatype="sport"]').all()
            print(f'   找到 {len(sports)} 个 Sport')
            
            if not sports:
                print('   ❌ 仍未找到 sport 节点')
                print()
                print('   检查页面中的所有 datatype 属性...')
                
                result = await page.evaluate('''
                    () => {
                        const elements = Array.from(document.querySelectorAll('[datatype]'));
                        return elements.map(el => ({
                            tag: el.tagName,
                            datatype: el.getAttribute('datatype'),
                            text: el.textContent.trim().substring(0, 30),
                            visible: el.offsetParent !== null
                        }));
                    }
                ''')
                
                print(f'   找到 {len(result)} 个带 datatype 的元素:')
                for item in result[:10]:
                    print(f'      {item}')
                
                return
            
            # 分析每个 sport
            print()
            print('5️⃣  Sport 列表:')
            for i, sport in enumerate(sports):
                sport_name = await sport.text_content()
                sport_id = await sport.get_attribute('data-navigation-id')
                is_visible = await sport.is_visible()
                print(f'   [{i+1}] {sport_name.strip()} (ID: {sport_id}, visible: {is_visible})')
            
            # 点击第一个可见的 sport
            print()
            print('6️⃣  展开第一个 Sport...')
            first_sport = sports[0]
            sport_name = await first_sport.text_content()
            
            print(f'   点击: {sport_name.strip()}')
            await first_sport.click()
            await asyncio.sleep(2)
            
            # 查找子节点
            print()
            print('7️⃣  查找子节点...')
            
            # Country
            countries = await page.locator('li[datatype="country"]').all()
            print(f'   Country: {len(countries)} 个')
            for i, country in enumerate(countries[:5]):
                text = await country.text_content()
                is_visible = await country.is_visible()
                print(f'      - {text.strip()} (visible: {is_visible})')
            
            # Competition
            competitions = await page.locator('li[datatype="competition"]').all()
            print(f'   Competition: {len(competitions)} 个')
            for i, comp in enumerate(competitions[:5]):
                text = await comp.text_content()
                is_visible = await comp.is_visible()
                print(f'      - {text.strip()} (visible: {is_visible})')
            
            # 点击第一个可见的 competition
            if competitions:
                visible_comps = [c for c in competitions if await c.is_visible()]
                if visible_comps:
                    first_comp = visible_comps[0]
                    comp_name = await first_comp.text_content()
                    print()
                    print(f'8️⃣  点击第一个 Competition: {comp_name.strip()}')
                    await first_comp.click()
                    await asyncio.sleep(2)
                    
                    # Events
                    events = await page.locator('li[data-navigation-type="EVENT"]').all()
                    visible_events = [e for e in events if await e.is_visible()]
                    print(f'   Event: {len(visible_events)} 个可见')
                    for i, event in enumerate(visible_events[:5]):
                        text = await event.text_content()
                        event_id = await event.get_attribute('data-navigation-id')
                        print(f'      - {text.strip()} (ID: {event_id})')
                    
                    # 点击第一个 event
                    if visible_events:
                        first_event = visible_events[0]
                        event_name = await first_event.text_content()
                        print()
                        print(f'9️⃣  点击第一个 Event: {event_name.strip()}')
                        await first_event.click()
                        await asyncio.sleep(2)
                        
                        # Markets
                        markets = await page.locator('li[datatype="market"]').all()
                        visible_markets = [m for m in markets if await m.is_visible()]
                        print(f'   Market: {len(visible_markets)} 个可见')
                        for i, market in enumerate(visible_markets[:10]):
                            text = await market.text_content()
                            market_id = await market.get_attribute('data-navigation-id')
                            print(f'      - {text.strip()} (ID: {market_id})')
            
            print()
            print('=' * 70)
            print('✅ 菜单结构分析完成')
            print('=' * 70)
            
            # 构建结构
            menu_structure = {
                'total_sports': len(sports),
                'total_countries': len(countries),
                'total_competitions': len(competitions),
                'sample_structure': {
                    'sport': sport_name.strip() if sports else None,
                    'competition': comp_name.strip() if competitions else None,
                    'event': event_name.strip() if visible_events else None,
                }
            }
            
            with open('menu_structure.json', 'w', encoding='utf-8') as f:
                json.dump(menu_structure, f, ensure_ascii=False, indent=2)
            
            print()
            print('✅ 已保存: menu_structure.json')
            
            await asyncio.sleep(10)
        
        except Exception as e:
            print(f'❌ 错误: {e}')
            import traceback
            traceback.print_exc()
        
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(test_menu_structure())
