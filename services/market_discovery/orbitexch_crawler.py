# -------------------------------------------------------------------------------------------------
#  OrbitExch 事件爬虫 - 修复版
# -------------------------------------------------------------------------------------------------

"""
修复内容:
1. 保存到 output/orbitexch_events.json
2. 保持原始 event 不变，只在 home_team 和 away_team 中拆分
"""

import asyncio
import logging
import re
from typing import List, Tuple
from dataclasses import dataclass, asdict
import json
from datetime import datetime
from pathlib import Path


@dataclass
class EventNode:
    """事件节点"""
    platform: str
    sport: str
    sport_id: str
    competition: str
    competition_id: str
    event: str           # 保持原始格式
    event_id: str
    home_team: str       # 拆分后的主队
    away_team: str       # 拆分后的客队
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
    
    @staticmethod
    def split_teams(event_str: str) -> Tuple[str, str]:
        """
        拆分 event 字符串为 home_team 和 away_team
        不修改原始 event，只用于提取队伍信息
        
        支持的分隔符: vs, vs., @, -, v
        示例:
        - "Navy @ Cincinnati" -> ("Cincinnati", "Navy")  @ 表示客场
        - "Team A vs Team B" -> ("Team A", "Team B")
        - "Arsenal - Chelsea" -> ("Arsenal", "Chelsea")
        """
        # 按优先级尝试不同的分隔符
        separators = [
            (r'\s+@\s+', True),      # @ 表示客场，需要交换
            (r'\s+vs\.?\s+', False), # vs 或 vs.
            (r'\s+-\s+', False),     # -
            (r'\s+v\s+', False),     # v
        ]
        
        for pattern, should_swap in separators:
            match = re.split(pattern, event_str, maxsplit=1, flags=re.IGNORECASE)
            if len(match) == 2:
                team1, team2 = match[0].strip(), match[1].strip()
                if should_swap:
                    # @ 表示 team1 @ team2，即 team1 是客队，team2 是主队
                    return team2, team1
                else:
                    return team1, team2
        
        # 如果都不匹配，返回原字符串和空字符串
        return event_str, ""
    
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
                
                # 拆分队伍（不修改原始 event_name）
                home_team, away_team = self.split_teams(event_name)
                
                # 创建事件节点
                event_node = EventNode(
                    platform="OrbitExch",
                    sport=sport_name,
                    sport_id=sport_id,
                    competition=comp_name,
                    competition_id=comp_id,
                    event=event_name,  # 保持原始格式
                    event_id=event_id,
                    home_team=home_team,
                    away_team=away_team,
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
    from playwright.async_api import async_playwright
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        
        try:
            # 直接访问公开页面（不需要登录）
            print('1️⃣  访问 OrbitExch...')
            await page.goto('https://orbitexch.com')
            await asyncio.sleep(3)
            
            # 创建爬虫
            print()
            print('=' * 70)
            print('2️⃣  开始爬取事件')
            print('=' * 70)
            print()
            
            crawler = OrbitExchEventCrawler(page)
            
            # 爬取第1个和第22个运动项目
            events = await crawler.crawl_sports([0, 23])
            
            # 保存结果到 output 目录
            print()
            print('=' * 70)
            print('3️⃣  保存结果')
            print('=' * 70)
            
            # 确保 output 目录存在
            output_dir = Path('output')
            output_dir.mkdir(exist_ok=True)
            
            events_dict = [e.to_dict() for e in events]
            
            output_file = output_dir / 'orbitexch_events.json'
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(events_dict, f, ensure_ascii=False, indent=2)
            
            print(f'✅ 已保存 {len(events)} 个事件到 {output_file}')
            
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
                print('示例事件 (前5个):')
                for event in events[:5]:
                    print(f'  原始: {event.event}')
                    print(f'    主队: {event.home_team}')
                    print(f'    客队: {event.away_team}')
                    print()
            
            await asyncio.sleep(5)
        
        except Exception as e:
            print(f'❌ 错误: {e}')
            import traceback
            traceback.print_exc()
        
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
