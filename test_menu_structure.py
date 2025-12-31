"""
分析 OrbitExch 左侧菜单结构
"""

import asyncio
from playwright.async_api import async_playwright
from dotenv import load_dotenv
import os
import json


async def analyze_menu():
    """分析菜单结构"""
    
    load_dotenv(encoding='utf-8')
    username = os.getenv('ORBITEXCH_USERNAME')
    password = os.getenv('ORBITEXCH_PASSWORD')
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        
        # 登录
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
        
        await page.goto('https://orbitexch.com/customer/inplay/highlights')
        await asyncio.sleep(3)
        
        print('=' * 70)
        print('分析左侧菜单结构')
        print('=' * 70)
        print()
        
        # 使用 JavaScript 分析菜单
        menu_info = await page.evaluate('''
            () => {
                // 查找所有可能的菜单项
                const allElements = Array.from(document.querySelectorAll('*'));
                
                // 查找有 data-type 属性的元素
                const withDataType = allElements.filter(el => el.hasAttribute('data-type'));
                
                return {
                    totalElements: allElements.length,
                    elementsWithDataType: withDataType.length,
                    dataTypes: withDataType.map(el => ({
                        tag: el.tagName,
                        dataType: el.getAttribute('data-type'),
                        text: el.textContent.trim().substring(0, 50),
                        classes: Array.from(el.classList).join(' '),
                        id: el.id,
                        parentTag: el.parentElement?.tagName
                    }))
                };
            }
        ''')
        
        print(f'页面总元素数: {menu_info["totalElements"]}')
        print(f'带 data-type 的元素: {menu_info["elementsWithDataType"]}')
        print()
        
        if menu_info['elementsWithDataType'] > 0:
            print('=' * 70)
            print('发现的 data-type 元素:')
            print('=' * 70)
            
            # 按 data-type 分组
            by_type = {}
            for item in menu_info['dataTypes']:
                dtype = item['dataType']
                if dtype not in by_type:
                    by_type[dtype] = []
                by_type[dtype].append(item)
            
            for dtype, items in sorted(by_type.items()):
                print(f'\n[{dtype}] - {len(items)} 个:')
                for item in items[:3]:  # 只显示前3个
                    print(f'  Tag: <{item["tag"]}>')
                    print(f'  Text: {item["text"]}')
                    print(f'  Classes: {item["classes"]}')
                    if item['id']:
                        print(f'  ID: {item["id"]}')
                    print()
        
        else:
            print('❌ 未找到 data-type 属性')
            print('\n尝试查找左侧菜单容器...')
            
            # 尝试找到侧边栏
            sidebar_info = await page.evaluate('''
                () => {
                    const selectors = [
                        'aside',
                        '[class*="sidebar"]',
                        '[class*="menu"]',
                        'nav',
                        '[role="navigation"]'
                    ];
                    
                    const results = [];
                    for (const selector of selectors) {
                        const elements = document.querySelectorAll(selector);
                        if (elements.length > 0) {
                            results.push({
                                selector: selector,
                                count: elements.length,
                                sample: {
                                    tag: elements[0].tagName,
                                    classes: Array.from(elements[0].classList).join(' '),
                                    children: elements[0].children.length
                                }
                            });
                        }
                    }
                    return results;
                }
            ''')
            
            for result in sidebar_info:
                print(f'\nSelector: {result["selector"]}')
                print(f'  找到: {result["count"]} 个')
                print(f'  示例: <{result["sample"]["tag"]}> class="{result["sample"]["classes"]}"')
                print(f'  子元素: {result["sample"]["children"]} 个')
        
        print()
        print('=' * 70)
        print('请观察左侧菜单，看看是否有 Sport 下拉菜单')
        print('然后手动点击几个菜单项，观察 URL 变化')
        print('=' * 70)
        print()
        
        # 等待用户操作
        print('按回车键继续截图...')
        input()
        
        # 截图保存
        await page.screenshot(path='menu_screenshot.png', full_page=True)
        print('✅ 截图已保存: menu_screenshot.png')
        
        # 保存 HTML
        html = await page.content()
        with open('menu_page.html', 'w', encoding='utf-8') as f:
            f.write(html)
        print('✅ HTML 已保存: menu_page.html')
        
        await browser.close()


if __name__ == '__main__':
    asyncio.run(analyze_menu())
