#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  市场发现服务 - 主程序
# -------------------------------------------------------------------------------------------------

"""
市场发现服务

1. 从 OrbitExch 爬取事件
2. 从 Polymarket 获取市场
3. 使用通用匹配器匹配市场
4. 保存结果
"""

import asyncio
import logging
import json
from pathlib import Path
from datetime import datetime

from orbitexch_crawler import OrbitExchEventCrawler
from polymarket_adapter import PolymarketAdapter
from universal_matcher import UniversalMatcher

from dotenv import load_dotenv
import os
from playwright.async_api import async_playwright


async def main():
    """主函数"""
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    log = logging.getLogger('MarketDiscovery')
    
    print('=' * 70)
    print('市场发现服务 - 通用匹配')
    print('=' * 70)
    print()
    
    # 创建输出目录
    output_dir = Path('./data/markets')
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. 爬取 OrbitExch
    print('1️⃣  爬取 OrbitExch 事件...')
    
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
        
        # 爬取
        crawler = OrbitExchEventCrawler(page)
        orbit_events = await crawler.crawl_sports([22])  # Soccer
        
        await browser.close()
    
    orbit_events_dict = [e.to_dict() for e in orbit_events]
    print(f'✅ 收集到 {len(orbit_events)} 个 OrbitExch 事件')
    
    # 2. 获取 Polymarket 市场
    print('\n2️⃣  获取 Polymarket 市场...')
    poly_adapter = PolymarketAdapter()
    poly_markets = await poly_adapter.discover_markets(category='sports')
    
    # 转换 Polymarket 数据格式以适配匹配器
    poly_events_dict = []
    for m in poly_markets:
        m_dict = m.to_dict()
        # 添加 event 字段（从 question 提取）
        m_dict['event'] = m_dict.get('question', '')
        # 添加 sport 字段（从 category 提取）
        m_dict['sport'] = m_dict.get('category', 'Unknown')
        # 添加 competition 字段（暂时为空）
        m_dict['competition'] = ''
        poly_events_dict.append(m_dict)
    
    print(f'✅ 收集到 {len(poly_events_dict)} 个 Polymarket 市场')
    
    # 3. 使用通用匹配器匹配
    print('\n3️⃣  匹配市场 (通用匹配器)...')
    matcher = UniversalMatcher()
    matches = matcher.match_events(orbit_events_dict, poly_events_dict, "OrbitExch", "Polymarket")
    print(f'✅ 找到 {len(matches)} 个有效匹配')
    
    # 4. 保存结果
    print('\n4️⃣  保存结果...')
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # 保存 OrbitExch 事件
    orbit_file = output_dir / f'orbitexch_events_{timestamp}.json'
    with open(orbit_file, 'w', encoding='utf-8') as f:
        json.dump(orbit_events_dict, f, ensure_ascii=False, indent=2)
    
    # 保存 Polymarket 市场
    poly_file = output_dir / f'polymarket_markets_{timestamp}.json'
    with open(poly_file, 'w', encoding='utf-8') as f:
        json.dump(poly_events_dict, f, ensure_ascii=False, indent=2)
    
    # 保存匹配结果
    matches_dict = [
        {
            'orbitexch': {
                'sport': m.platform1_event.get('sport'),
                'competition': m.platform1_event.get('competition'),
                'event': m.platform1_event.get('event'),
                'event_id': m.platform1_event.get('event_id'),
            },
            'polymarket': {
                'question': m.platform2_event.get('question'),
                'market_id': m.platform2_event.get('market_id'),
                'category': m.platform2_event.get('category'),
            },
            'match_quality': {
                'sport_match': m.sport_match,
                'competition_match': m.competition_match,
                'home_similarity': m.home_similarity,
                'away_similarity': m.away_similarity,
                'total_similarity': m.event_similarity,
            }
        }
        for m in matches
    ]
    
    matches_file = output_dir / f'matches_{timestamp}.json'
    with open(matches_file, 'w', encoding='utf-8') as f:
        json.dump(matches_dict, f, ensure_ascii=False, indent=2)
    
    print(f'✅ 保存到 {output_dir}/')
    
    # 5. 显示摘要
    print('\n' + '=' * 70)
    print('摘要')
    print('=' * 70)
    print(f'OrbitExch 事件: {len(orbit_events)}')
    print(f'Polymarket 市场: {len(poly_events_dict)}')
    print(f'有效匹配: {len(matches)}')
    
    if matches:
        print('\n匹配示例:')
        for match in matches[:5]:
            print(f'\n  OrbitExch: {match.platform1_event["event"]}')
            print(f'  Polymarket: {match.platform2_event.get("question", "N/A")}')
            print(f'  质量: home={match.home_similarity}, away={match.away_similarity}')


if __name__ == '__main__':
    asyncio.run(main())
