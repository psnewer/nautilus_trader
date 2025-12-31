# -------------------------------------------------------------------------------------------------
#  OrbitExch 事件爬虫 - 完整版
# -------------------------------------------------------------------------------------------------

"""
爬取逻辑:
1. 点击 Sport
2. 如果有 "All {Sport}" 链接:
   - 点击进入，收集所有 Competition
   - 逐个处理 Competition
3. 如果没有 "All {Sport}" 链接:
   - 直接在菜单中查找 Competition
   - 逐个点击处理
"""

import asyncio
import logging
from typing import List
from dataclasses import dataclass, asdict
import json
from datetime import datetime


@dataclass
class EventNode:
    """事件节点"""
    sport: str
    sport_id: str
    competition: str
    competition_id: str
    event: str
    event_id: str
    discovered_at: str
    
    def to_dict(self):
        return asdict(self)
    
    def __repr__(self):
        return f'{self.sport}/{self.competition}/{self.event}'


class OrbitExchEventCrawler:
    """OrbitExch 事件爬虫"""
    
    def __init__(self, page):
        self.page = page
        self._log = logging.getLogger('EventCrawler')
        self.events: List[EventNode] = []
    
    async def crawl_sports(self, sport_indices: List[int]) -> List[EventNode]:
        """爬取指定的运动项目"""
        self._log.info(f'🔍 开始爬取 {len(sport_indices)} 个运动项目...')
        
        # 记住初始 URL
        self.initial_url = self.page.url
        
        # 确保 Sports 区域展开
        await self._ensure_sports_expanded()
        
        # 获取所有 sport
        all_sports = await self.page.locator('li[datatype="sport"]').all()
        self._log.info(f'   找到 {len(all_sports)} 个运动项目')
        
        # 收集指定索引的 sport 信息
        sport_infos = []
        for idx in sport_indices:
            if idx >= len(all_sports):
                continue
            
            sport_elem = all_sports[idx]
            sport_name = (await sport_elem.text_content()).strip()
            sport_id = await sport_elem.get_attribute('data-navigation-id')
            
            sport_infos.append({
                'index': idx,
                'name': sport_name,
                'id': sport_id,
            })
        
        # 爬取每个 sport
        for i, sport_info in enumerate(sport_infos, 1):
            self._log.info(f'[{i}/{len(sport_infos)}] 爬取: {sport_info["name"]}')
            
            # 确保在初始页面
            if self.page.url != self.initial_url:
                await self.page.goto(self.initial_url)
                await asyncio.sleep(2)
                await self._ensure_sports_expanded()
            
            await self._crawl_sport(sport_info['index'], sport_info['name'], sport_info['id'])
        
        self._log.info(f'✅ 爬取完成，共 {len(self.events)} 个事件')
        return self.events
    
    async def _ensure_sports_expanded(self):
        """确保 Sports 区域展开"""
        sports_section = self.page.locator('#a-sportsSection')
        classes = await sports_section.get_attribute('class')
        if 'biab_opened' not in classes:
            await sports_section.click()
            await asyncio.sleep(1)
    
    async def _crawl_sport(self, sport_index: int, sport_name: str, sport_id: str):
        """爬取单个运动项目"""
        
        # 点击 sport 展开
        sport_elem = self.page.locator('li[datatype="sport"]').nth(sport_index)
        
        try:
            await sport_elem.click()
            await asyncio.sleep(1)
        except Exception as e:
            self._log.error(f'   点击失败: {e}')
            return
        
        # 查找 "All {Sport}" 链接
        all_link = self.page.locator(f'li[class*="List"] a:has-text("All {sport_name}")').first
        
        if await all_link.count() > 0:
            self._log.info(f'   找到 "All {sport_name}" 链接')
            await self._crawl_sport_with_all_link(sport_index, sport_name, sport_id)
        else:
            self._log.info(f'   未找到 "All" 链接，直接遍历 Competitions')
            await self._crawl_sport_without_all_link(sport_index, sport_name, sport_id)
    
    async def _crawl_sport_with_all_link(
        self,
        sport_index: int,
        sport_name: str,
        sport_id: str
    ):
        """有 "All Sport" 链接的情况"""
        
        all_link = self.page.locator(f'li[class*="List"] a:has-text("All {sport_name}")').first
        
        try:
            # 点击进入 "All Sport" 页面
            await all_link.click()
            await self.page.wait_for_load_state('networkidle', timeout=10000)
            await asyncio.sleep(1)
            
            # 收集所有 competition 信息
            comp_infos = await self._collect_competition_infos()
            self._log.info(f'   收集到 {len(comp_infos)} 个 Competitions')
            
            # 后退到初始页面
            await self.page.go_back()
            await asyncio.sleep(1)
            await self._ensure_sports_expanded()
            
            # 逐个处理 competition
            await self._process_competitions_with_all_link(
                sport_index,
                sport_name,
                sport_id,
                comp_infos
            )
            
        except Exception as e:
            self._log.error(f'   处理失败: {e}')
    
    async def _crawl_sport_without_all_link(
        self,
        sport_index: int,
        sport_name: str,
        sport_id: str
    ):
        """没有 "All Sport" 链接的情况 - 直接在菜单遍历"""
        
        # 收集当前菜单中所有 competition 信息
        comp_elems = await self.page.locator('li[datatype="competition"]:visible').all()
        
        comp_infos = []
        for comp_elem in comp_elems:
            try:
                comp_name = (await comp_elem.text_content()).strip()
                comp_id = await comp_elem.get_attribute('data-navigation-id')
                comp_infos.append({'name': comp_name, 'id': comp_id})
            except:
                pass
        
        self._log.info(f'   找到 {len(comp_infos)} 个 Competitions')
        
        # 逐个处理 competition
        await self._process_competitions_without_all_link(
            sport_index,
            sport_name,
            sport_id,
            comp_infos[:5]  # 限制最多5个
        )
    
    async def _process_competitions_with_all_link(
        self,
        sport_index: int,
        sport_name: str,
        sport_id: str,
        comp_infos: List[dict]
    ):
        """处理 competitions (有 All 链接)"""
        
        for i, comp_info in enumerate(comp_infos, 1):
            self._log.info(f'      [{i}/{len(comp_infos)}] Competition: {comp_info["name"]}')
            
            # 确保在初始页面
            if self.page.url != self.initial_url:
                await self.page.goto(self.initial_url)
                await asyncio.sleep(2)
                await self._ensure_sports_expanded()
            
            # 重新点击 sport
            sport_elem = self.page.locator('li[datatype="sport"]').nth(sport_index)
            try:
                await sport_elem.click()
                await asyncio.sleep(1)
            except:
                continue
            
            # 重新点击 "All {Sport}"
            all_link = self.page.locator(f'li[class*="List"] a:has-text("All {sport_name}")').first
            if await all_link.count() > 0:
                try:
                    await all_link.click()
                    await self.page.wait_for_load_state('networkidle', timeout=10000)
                    await asyncio.sleep(1)
                except:
                    continue
            else:
                continue
            
            # 点击 competition
            comp_elem = self.page.locator(f'li[data-navigation-id="{comp_info["id"]}"]').first
            try:
                await comp_elem.click()
                await self.page.wait_for_load_state('networkidle', timeout=10000)
                await asyncio.sleep(1)
            except:
                continue
            
            # 收集 events
            await self._collect_events_from_current_page(
                sport_name,
                sport_id,
                comp_info['name'],
                comp_info['id']
            )
    
    async def _process_competitions_without_all_link(
        self,
        sport_index: int,
        sport_name: str,
        sport_id: str,
        comp_infos: List[dict]
    ):
        """处理 competitions (没有 All 链接) - 直接点击"""
        
        for i, comp_info in enumerate(comp_infos, 1):
            self._log.info(f'      [{i}/{len(comp_infos)}] Competition: {comp_info["name"]}')
            
            # 确保在初始页面
            if self.page.url != self.initial_url:
                await self.page.goto(self.initial_url)
                await asyncio.sleep(2)
                await self._ensure_sports_expanded()
            
            # 重新点击 sport
            sport_elem = self.page.locator('li[datatype="sport"]').nth(sport_index)
            try:
                await sport_elem.click()
                await asyncio.sleep(1)
            except:
                continue
            
            # 直接点击 competition
            comp_elem = self.page.locator(f'li[data-navigation-id="{comp_info["id"]}"]').first
            try:
                await comp_elem.click()
                await self.page.wait_for_load_state('networkidle', timeout=10000)
                await asyncio.sleep(1)
            except Exception as e:
                self._log.error(f'         点击失败: {e}')
                continue
            
            # 收集 events
            await self._collect_events_from_current_page(
                sport_name,
                sport_id,
                comp_info['name'],
                comp_info['id']
            )
    
    async def _collect_competition_infos(self) -> List[dict]:
        """收集当前页面所有 competition 信息"""
        comp_elems = await self.page.locator('li[datatype="competition"]:visible').all()
        
        comp_infos = []
        for comp_elem in comp_elems:
            try:
                comp_name = (await comp_elem.text_content()).strip()
                comp_id = await comp_elem.get_attribute('data-navigation-id')
                comp_infos.append({'name': comp_name, 'id': comp_id})
            except:
                pass
        
        return comp_infos[:5]  # 限制最多5个
    
    async def _collect_events_from_current_page(
        self,
        sport_name: str,
        sport_id: str,
        comp_name: str,
        comp_id: str
    ):
        """从当前页面收集所有 Events"""
        
        await asyncio.sleep(1)
        
        # 查找所有可见的 events
        event_elems = await self.page.locator('li[data-navigation-type="EVENT"]:visible').all()
        
        event_count = 0
        for event_elem in event_elems:
            try:
                event_name = (await event_elem.text_content()).strip()
                event_id = await event_elem.get_attribute('data-navigation-id')
                
                # 创建事件节点
                event_node = EventNode(
                    sport=sport_name,
                    sport_id=sport_id,
                    competition=comp_name,
                    competition_id=comp_id,
                    event=event_name,
                    event_id=event_id,
                    discovered_at=datetime.now().isoformat(),
                )
                
                self.events.append(event_node)
                event_count += 1
            except:
                pass
        
        self._log.info(f'         收集到 {event_count} 个 Events')


async def main():
    """主函数"""
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
            
            # 创建爬虫
            print()
            print('=' * 70)
            print('开始爬取事件')
            print('=' * 70)
            print()
            
            crawler = OrbitExchEventCrawler(page)
            
            # 爬取第1个和第23个运动项目
            events = await crawler.crawl_sports([0, 21])
            
            # 保存结果
            print()
            print('=' * 70)
            print('保存结果')
            print('=' * 70)
            
            events_dict = [e.to_dict() for e in events]
            
            with open('orbitexch_events.json', 'w', encoding='utf-8') as f:
                json.dump(events_dict, f, ensure_ascii=False, indent=2)
            
            print(f'✅ 已保存 {len(events)} 个事件到 orbitexch_events.json')
            
            # 统计
            if events:
                sports = set(e.sport for e in events)
                competitions = set(e.competition for e in events)
                
                print()
                print('统计:')
                print(f'  运动项目: {len(sports)}')
                print(f'  赛事: {len(competitions)}')
                print(f'  比赛: {len(events)}')
                
                print()
                print('示例事件:')
                for event in events[:10]:
                    print(f'  {event}')
            
            await asyncio.sleep(5)
        
        except Exception as e:
            print(f'❌ 错误: {e}')
            import traceback
            traceback.print_exc()
        
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
