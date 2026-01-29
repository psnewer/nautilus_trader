#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
市场发现集成测试 - 显示实际抓取的赛事

运行方式:
    PYTHONPATH=src/arbitrage python tests/unit_tests/services/market_discovery/test_integration.py

    # 测试 Polymarket EPL
    PYTHONPATH=src/arbitrage python tests/unit_tests/services/market_discovery/test_integration.py --polymarket

    # 测试 OrbitExch EPL
    PYTHONPATH=src/arbitrage python tests/unit_tests/services/market_discovery/test_integration.py --orbitexch

    # 测试所有平台
    PYTHONPATH=src/arbitrage python tests/unit_tests/services/market_discovery/test_integration.py --all
"""
import asyncio
import logging
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent / "src" / "arbitrage"))

from services.market_discovery.polymarket_scraper import PolymarketScraper
from services.market_discovery.orbitexch_scraper import OrbitExchScraper
from services.market_discovery.config import PolymarketVenueConfig, OrbitExchVenueConfig, SportConfig

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%H:%M:%S'
)
logging.getLogger('httpx').setLevel(logging.WARNING)


async def test_polymarket_discovery():
    """测试 Polymarket 市场发现"""
    print('=' * 70)
    print('Polymarket 市场发现 - 集成测试')
    print('=' * 70)

    config = PolymarketVenueConfig(enabled=True)
    scraper = PolymarketScraper(config)

    print('\n[1] 抓取体育赛事...\n')

    try:
        events = await scraper.discover_events()

        print(f'共发现 {len(events)} 场比赛\n')

        # 按 sport 分组
        by_sport = {}
        for e in events:
            by_sport.setdefault(e.sport, []).append(e)

        for sport, matches in sorted(by_sport.items(), key=lambda x: -len(x[1])):
            print(f'[{sport}] - {len(matches)} 场比赛')
            for m in matches[:5]:
                print(f'    {m.home_team} vs {m.away_team}')
            if len(matches) > 5:
                print(f'    ... 还有 {len(matches) - 5} 场')
            print()

        return events

    except Exception as e:
        print(f'错误: {e}')
        import traceback
        traceback.print_exc()
        return []


async def test_single_series():
    """测试单个 series (EPL) 的抓取"""
    import httpx

    print('=' * 70)
    print('EPL Series 抓取测试')
    print('=' * 70)

    print('\n抓取 EPL (series 10188)...\n')

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get('https://gamma-api.polymarket.com/series/10188')
            resp.raise_for_status()
            data = resp.json()

            events = data.get('events', [])
            open_events = [e for e in events if not e.get('closed', True)]

            print(f'总赛事: {len(events)}, 进行中: {len(open_events)}\n')

            print('进行中的比赛:')
            count = 0
            for e in open_events:
                title = e.get('title', '')
                if 'More Markets' in title or title.lower().startswith('draft '):
                    continue
                print(f'  - {title}')
                count += 1
                if count >= 15:
                    print(f'  ... 还有更多')
                    break

    except Exception as e:
        print(f'错误: {e}')


async def test_orbitexch_discovery():
    """测试 OrbitExch 市场发现 (EPL)"""
    print('=' * 70)
    print('OrbitExch 市场发现 - EPL')
    print('=' * 70)

    config = OrbitExchVenueConfig(
        enabled=True,
        sports=[
            SportConfig(sport='soccer', competitions=['English Premier League']),
        ],
    )

    scraper = OrbitExchScraper(config)

    try:
        print('\n[1] 启动浏览器...')
        await scraper.start_browser()

        print('\n[2] 抓取比赛...')
        events = await scraper.discover_events()

        print(f'\n[3] 结果: 发现 {len(events)} 场比赛')
        for e in events[:15]:
            print(f'    {e.home_team} vs {e.away_team} ({e.competition})')
        if len(events) > 15:
            print(f'    ... 还有 {len(events) - 15} 场')

        return events

    except Exception as e:
        print(f'\n错误: {e}')
        import traceback
        traceback.print_exc()
        return []

    finally:
        print('\n[4] 关闭浏览器...')
        await scraper.close_browser()


async def test_orbitexch_all():
    """测试 OrbitExch 所有体育赛事"""
    print('=' * 70)
    print('OrbitExch 全部赛事发现')
    print('=' * 70)

    # 配置多个 sports
    config = OrbitExchVenueConfig(
        enabled=True,
        sports=[
            SportConfig(sport='Soccer', competitions=[
                'English Premier League',
                'Spanish La Liga',
                'Italian Serie A',
                'German Bundesliga',
                'UEFA Champions League',
            ]),
            SportConfig(sport='Basketball', competitions=['NBA']),
            SportConfig(sport='Tennis', competitions=['Men\'s Australian Open 2026']),
            SportConfig(sport='Cricket', competitions=['Big Bash League']),
        ],
    )

    scraper = OrbitExchScraper(config)

    try:
        print('\n[1] 启动浏览器...')
        await scraper.start_browser()

        print('\n[2] 抓取比赛...')
        events = await scraper.discover_events()

        print(f'\n[3] 结果: 发现 {len(events)} 场比赛')

        # 按 sport 分组显示
        by_sport = {}
        for e in events:
            by_sport.setdefault(e.sport, []).append(e)

        for sport, matches in by_sport.items():
            print(f'\n    [{sport}] {len(matches)} 场')
            for m in matches[:5]:
                print(f'      - {m.home_team} vs {m.away_team} ({m.competition})')
            if len(matches) > 5:
                print(f'      ... 还有 {len(matches) - 5} 场')

        return events

    except Exception as e:
        print(f'\n错误: {e}')
        import traceback
        traceback.print_exc()
        return []

    finally:
        print('\n[4] 关闭浏览器...')
        await scraper.close_browser()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='市场发现集成测试')
    parser.add_argument('--polymarket', action='store_true', help='测试 Polymarket EPL')
    parser.add_argument('--orbitexch', action='store_true', help='测试 OrbitExch EPL')
    parser.add_argument('--orbitexch-all', action='store_true', help='测试 OrbitExch 所有赛事')
    parser.add_argument('--all', action='store_true', help='测试所有平台')
    args = parser.parse_args()

    if args.orbitexch_all:
        asyncio.run(test_orbitexch_all())
    elif args.orbitexch:
        asyncio.run(test_orbitexch_discovery())
    elif args.polymarket:
        asyncio.run(test_single_series())
    elif args.all:
        asyncio.run(test_polymarket_discovery())
        asyncio.run(test_orbitexch_all())
    else:
        # 默认测试 OrbitExch EPL
        asyncio.run(test_orbitexch_discovery())
