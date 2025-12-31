# -------------------------------------------------------------------------------------------------
#  OrbitExch 菜单爬虫 - 递归遍历
# -------------------------------------------------------------------------------------------------

"""
递归遍历 OrbitExch 左侧菜单

HTML 结构:
<li datatype="sport">Soccer</li>
<li datatype="country">GBR</li>
<li datatype="competition">English Premier League</li>
<li data-navigation-type="EVENT">Brentford v Tottenham</li>
<li datatype="market">Match Odds</li>
"""

import asyncio
import logging
from typing import List, Dict, Optional
from dataclasses import dataclass, field, asdict
import json


@dataclass
class MenuNode:
    """菜单节点"""
    node_type: Optional[str]  # 'sport', 'competition', 'country', 'event', 'market'
    name: str
    navigation_type: Optional[str]  # 'EVENT_TYPE', 'COUNTRY', 'COMPETITION', 'EVENT', 'MARKET'
    navigation_id: Optional[str]
    level: int
    children: List['MenuNode'] = field(default_factory=list)
    
    def to_dict(self):
        d = asdict(self)
        d['children'] = [c.to_dict() for c in self.children]
        return d
    
    def __repr__(self):
        type_str = f'[{self.node_type}]' if self.node_type else '[event]'
        return f'{"  " * self.level}{type_str} {self.name}'


class OrbitExchMenuCrawler:
    """OrbitExch 菜单爬虫"""
    
    def __init__(self, page):
        self.page = page
        self._log = logging.getLogger('MenuCrawler')
    
    async def crawl_full_menu(self) -> List[MenuNode]:
        """
        爬取完整菜单树
        
        Returns
        -------
        List[MenuNode]
            所有 sport 节点（每个包含完整子树）
        """
        self._log.info('�� 开始爬取 OrbitExch 菜单...')
        
        menu_tree = []
        
        # 查找所有 sport 节点
        sports = await self.page.locator('li[datatype="sport"]').all()
        self._log.info(f'   找到 {len(sports)} 个运动项目')
        
        for sport in sports:
            sport_name = await sport.text_content()
            sport_node = MenuNode(
                node_type='sport',
                name=sport_name.strip(),
                navigation_type=await sport.get_attribute('data-navigation-type'),
                navigation_id=await sport.get_attribute('data-navigation-id'),
                level=0,
            )
            
            self._log.info(f'{sport_node}')
            
            # 点击展开
            try:
                await sport.click(timeout=1000)
                await asyncio.sleep(0.5)
            except:
                pass
            
            # 递归爬取子节点
            await self._crawl_children(sport_node)
            
            menu_tree.append(sport_node)
        
        self._log.info(f'✅ 爬取完成')
        return menu_tree
    
    async def _crawl_children(self, parent_node: MenuNode):
        """递归爬取子节点"""
        
        # 根据父节点类型确定子节点类型
        if parent_node.node_type == 'sport':
            child_types = ['country', 'competition']
        elif parent_node.node_type == 'country':
            child_types = ['competition']
        elif parent_node.node_type == 'competition':
            # Event 没有 datatype，用 data-navigation-type="EVENT"
            await self._crawl_events(parent_node)
            return
        elif parent_node.node_type is None:  # Event
            child_types = ['market']
        else:
            return  # market 是叶子节点
        
        # 查找子节点
        for child_type in child_types:
            selector = f'li[datatype="{child_type}"]'
            children = await self.page.locator(selector).all()
            
            for child in children:
                child_name = await child.text_content()
                child_node = MenuNode(
                    node_type=child_type,
                    name=child_name.strip(),
                    navigation_type=await child.get_attribute('data-navigation-type'),
                    navigation_id=await child.get_attribute('data-navigation-id'),
                    level=parent_node.level + 1,
                )
                
                if len(parent_node.children) < 3:  # 只log前几个
                    self._log.info(f'{child_node}')
                
                # 点击展开
                try:
                    await child.click(timeout=1000)
                    await asyncio.sleep(0.3)
                except:
                    pass
                
                # 递归
                await self._crawl_children(child_node)
                
                parent_node.children.append(child_node)
    
    async def _crawl_events(self, competition_node: MenuNode):
        """爬取 Event 节点（没有 datatype 属性）"""
        
        # Event 使用 data-navigation-type="EVENT"
        events = await self.page.locator('li[data-navigation-type="EVENT"]').all()
        
        for event in events:
            event_name = await event.text_content()
            event_node = MenuNode(
                node_type=None,  # Event 没有 datatype
                name=event_name.strip(),
                navigation_type='EVENT',
                navigation_id=await event.get_attribute('data-navigation-id'),
                level=competition_node.level + 1,
            )
            
            if len(competition_node.children) < 3:
                self._log.info(f'{event_node}')
            
            # 点击展开
            try:
                await event.click(timeout=1000)
                await asyncio.sleep(0.3)
            except:
                pass
            
            # 爬取 markets
            await self._crawl_children(event_node)
            
            competition_node.children.append(event_node)


async def test_crawler():
    """测试爬虫"""
    import logging
    from dotenv import load_dotenv
    import os
    from playwright.async_api import async_playwright
    
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
        await asyncio.sleep(2)
        
        # 创建爬虫
        crawler = OrbitExchMenuCrawler(page)
        
        # 爬取（只爬第一个 sport）
        print()
        print('=' * 70)
        print('测试爬取第一个 Sport')
        print('=' * 70)
        
        sports = await page.locator('li[datatype="sport"]').all()
        if sports:
            first_sport = sports[0]
            sport_name = await first_sport.text_content()
            sport_node = MenuNode(
                node_type='sport',
                name=sport_name.strip(),
                navigation_type=await first_sport.get_attribute('data-navigation-type'),
                navigation_id=await first_sport.get_attribute('data-navigation-id'),
                level=0,
            )
            
            print(f'{sport_node}')
            
            # 点击展开
            await first_sport.click()
            await asyncio.sleep(1)
            
            # 爬取子节点
            await crawler._crawl_children(sport_node)
            
            # 保存结果
            with open('menu_tree.json', 'w', encoding='utf-8') as f:
                json.dump(sport_node.to_dict(), f, ensure_ascii=False, indent=2)
            
            print()
            print(f'✅ 已保存: menu_tree.json')
            print(f'   Sport: {sport_node.name}')
            print(f'   子节点: {len(sport_node.children)}')
        
        await asyncio.sleep(5)
        await browser.close()


if __name__ == '__main__':
    asyncio.run(test_crawler())
