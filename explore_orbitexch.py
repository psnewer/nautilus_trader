"""
OrbitExch 网站结构探索

自动分析 https://orbitexch.com 的页面结构
"""

import asyncio
import json
from playwright.async_api import async_playwright
from pathlib import Path


async def explore_orbitexch():
    """探索 OrbitExch 网站结构"""
    
    # 创建输出目录
    Path('docs/screenshots').mkdir(parents=True, exist_ok=True)
    Path('docs/html').mkdir(parents=True, exist_ok=True)
    
    async with async_playwright() as p:
        print('=' * 70)
        print('OrbitExch 网站结构探索')
        print('=' * 70)
        print()
        
        # 启动浏览器
        browser = await p.chromium.launch(
            headless=False,  # 显示浏览器
            args=['--start-maximized']
        )
        
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        
        page = await context.new_page()
        
        # ========== 1. 探索首页/登录页 ==========
        print('1️⃣  访问首页...')
        await page.goto('https://orbitexch.com', wait_until='networkidle')
        await page.screenshot(path='docs/screenshots/01_homepage.png', full_page=True)
        print('   ✅ 截图保存: docs/screenshots/01_homepage.png')
        
        # 保存 HTML
        html = await page.content()
        with open('docs/html/01_homepage.html', 'w', encoding='utf-8') as f:
            f.write(html)
        print('   ✅ HTML 保存: docs/html/01_homepage.html')
        
        # 分析登录元素
        print()
        print('2️⃣  分析登录元素...')
        
        login_info = {
            'username_fields': [],
            'password_fields': [],
            'login_buttons': [],
        }
        
        # 查找用户名输入框
        username_selectors = [
            'input[name*="user" i]',
            'input[name*="login" i]',
            'input[name*="email" i]',
            'input[id*="user" i]',
            'input[id*="login" i]',
            'input[type="text"]',
            'input[type="email"]',
        ]
        
        for selector in username_selectors:
            elements = await page.query_selector_all(selector)
            for elem in elements:
                name = await elem.get_attribute('name')
                id_attr = await elem.get_attribute('id')
                placeholder = await elem.get_attribute('placeholder')
                
                info = {
                    'selector': selector,
                    'name': name,
                    'id': id_attr,
                    'placeholder': placeholder,
                }
                
                if info not in login_info['username_fields']:
                    login_info['username_fields'].append(info)
        
        # 查找密码输入框
        password_elements = await page.query_selector_all('input[type="password"]')
        for elem in password_elements:
            name = await elem.get_attribute('name')
            id_attr = await elem.get_attribute('id')
            
            login_info['password_fields'].append({
                'name': name,
                'id': id_attr,
            })
        
        # 查找登录按钮
        button_selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Login")',
            'button:has-text("Sign in")',
            'button:has-text("Log in")',
        ]
        
        for selector in button_selectors:
            elements = await page.query_selector_all(selector)
            for elem in elements:
                text = await elem.inner_text()
                login_info['login_buttons'].append({
                    'selector': selector,
                    'text': text,
                })
        
        print('   登录元素分析:')
        print(f'   - 用户名框: {len(login_info["username_fields"])} 个')
        for field in login_info['username_fields']:
            print(f'     • name={field["name"]}, id={field["id"]}, placeholder={field["placeholder"]}')
        
        print(f'   - 密码框: {len(login_info["password_fields"])} 个')
        for field in login_info['password_fields']:
            print(f'     • name={field["name"]}, id={field["id"]}')
        
        print(f'   - 登录按钮: {len(login_info["login_buttons"])} 个')
        for btn in login_info['login_buttons']:
            print(f'     • {btn["selector"]}: "{btn["text"]}"')
        
        # 保存登录信息
        with open('docs/login_elements.json', 'w', encoding='utf-8') as f:
            json.dump(login_info, f, indent=2, ensure_ascii=False)
        
        # ========== 3. 探索 InPlay 页面 ==========
        print()
        print('3️⃣  访问 InPlay 页面...')
        await page.goto('https://orbitexch.com/customer/inplay/highlights', wait_until='networkidle')
        await asyncio.sleep(3)  # 等待动态内容加载
        
        await page.screenshot(path='docs/screenshots/02_inplay.png', full_page=True)
        print('   ✅ 截图保存: docs/screenshots/02_inplay.png')
        
        html = await page.content()
        with open('docs/html/02_inplay.html', 'w', encoding='utf-8') as f:
            f.write(html)
        print('   ✅ HTML 保存: docs/html/02_inplay.html')
        
        # 分析赛事结构
        print()
        print('4️⃣  分析赛事结构...')
        
        # 查找赛事容器
        event_containers = await page.query_selector_all('[class*="event" i], [class*="match" i], [class*="game" i]')
        print(f'   - 找到可能的赛事容器: {len(event_containers)} 个')
        
        # 查找赔率元素
        odds_elements = await page.query_selector_all('[class*="odd" i], [class*="price" i], [class*="rate" i]')
        print(f'   - 找到可能的赔率元素: {len(odds_elements)} 个')
        
        # 提取页面上的所有类名（用于理解页面结构）
        class_names = await page.evaluate('''() => {
            const elements = document.querySelectorAll('*');
            const classes = new Set();
            elements.forEach(el => {
                if (el.className && typeof el.className === 'string') {
                    el.className.split(' ').forEach(cls => {
                        if (cls && cls.length > 0) {
                            classes.add(cls);
                        }
                    });
                }
            });
            return Array.from(classes);
        }''')
        
        # 过滤出相关的类名
        relevant_classes = [
            cls for cls in class_names 
            if any(keyword in cls.lower() for keyword in [
                'event', 'match', 'game', 'sport', 
                'odd', 'price', 'bet', 'rate',
                'market', 'selection', 'runner',
                'back', 'lay'
            ])
        ]
        
        print(f'   - 相关 CSS 类名 ({len(relevant_classes)} 个):')
        for cls in sorted(relevant_classes)[:20]:
            print(f'     • {cls}')
        
        with open('docs/css_classes.json', 'w', encoding='utf-8') as f:
            json.dump(relevant_classes, f, indent=2)
        
        # ========== 5. 分析页面导航 ==========
        print()
        print('5️⃣  分析页面导航...')
        
        links = await page.query_selector_all('a[href]')
        navigation = {}
        
        for link in links[:50]:  # 只分析前 50 个链接
            text = (await link.inner_text()).strip()
            href = await link.get_attribute('href')
            
            if text and href and len(text) < 50:
                navigation[text] = href
        
        print('   主要导航链接:')
        for text, href in list(navigation.items())[:15]:
            print(f'     • {text}: {href}')
        
        with open('docs/navigation.json', 'w', encoding='utf-8') as f:
            json.dump(navigation, f, indent=2, ensure_ascii=False)
        
        # ========== 6. 等待观察 ==========
        print()
        print('=' * 70)
        print('浏览器将保持打开 60 秒，请手动探索页面')
        print('提示: 你可以手动点击、滚动，观察页面结构')
        print('=' * 70)
        
        await asyncio.sleep(60)
        
        await browser.close()
        
        print()
        print('✅ 探索完成！')
        print()
        print('生成的文件:')
        print('  - docs/screenshots/*.png  (页面截图)')
        print('  - docs/html/*.html        (页面 HTML)')
        print('  - docs/login_elements.json')
        print('  - docs/css_classes.json')
        print('  - docs/navigation.json')


if __name__ == '__main__':
    asyncio.run(explore_orbitexch())
