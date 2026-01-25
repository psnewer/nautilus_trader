"""
Polymarket 市场发现测试脚本

使用方法:
    # 先安装依赖
    pip install playwright httpx
    playwright install chromium

    # 运行测试
    python -m src.arbitrage.services.market_discovery.test_polymarket
"""

import asyncio
import logging

from .config import PolymarketVenueConfig, PolymarketApiConfig, BrowserConfig
from .polymarket_scraper import PolymarketScraper

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("PolymarketTest")


async def test_sport_competition_mapping():
    """测试 sport-competition 映射抓取"""
    logger.info("=" * 60)
    logger.info("测试 1: Sport-Competition 映射抓取")
    logger.info("=" * 60)

    config = PolymarketVenueConfig(
        enabled=True,
        browser=BrowserConfig(headless=False, timeout_ms=30000),  # 显示浏览器便于调试
        api=PolymarketApiConfig(),
    )

    scraper = PolymarketScraper(config, logger)

    try:
        await scraper.start_browser()
        mappings = await scraper.scrape_sport_competition_mapping()

        logger.info(f"\n找到 {len(mappings)} 个 sport-competition 映射:")
        for m in mappings[:10]:  # 只显示前10个
            logger.info(f"  - {m.sport} / {m.competition}")

        if len(mappings) > 10:
            logger.info(f"  ... 还有 {len(mappings) - 10} 个")

        return mappings

    finally:
        await scraper.close_browser()


async def test_gamma_api():
    """测试 Gamma API 数据获取"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 2: Gamma API Sports 数据")
    logger.info("=" * 60)

    config = PolymarketVenueConfig(
        enabled=True,
        browser=BrowserConfig(headless=True),
        api=PolymarketApiConfig(),
    )

    scraper = PolymarketScraper(config, logger)

    try:
        sports_data = await scraper.fetch_sports_tags()
        logger.info(f"\nGamma API 返回 {len(sports_data)} 个 sports 条目")

        # 统计 tags
        tag_counts = scraper._count_tag_occurrences(sports_data)
        eligible_tags = [(tid, c) for tid, c in tag_counts.items() if c <= 2]

        logger.info(f"总共 {len(tag_counts)} 个唯一 tags")
        logger.info(f"其中 {len(eligible_tags)} 个 tags 出现次数 <= 2（符合遍历条件）")

        # 显示部分 sports 数据结构
        if sports_data:
            logger.info("\n第一个 sport 数据结构示例:")
            first = sports_data[0]
            for key in list(first.keys())[:5]:
                logger.info(f"  {key}: {first.get(key)}")

        return sports_data

    except Exception as e:
        logger.error(f"Gamma API 调用失败: {e}")
        return []


async def test_event_discovery():
    """测试完整的事件发现流程"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 3: 完整事件发现流程")
    logger.info("=" * 60)

    config = PolymarketVenueConfig(
        enabled=True,
        browser=BrowserConfig(headless=False, timeout_ms=30000),
        api=PolymarketApiConfig(events_limit=50),  # 限制获取数量
    )

    scraper = PolymarketScraper(config, logger)

    try:
        await scraper.start_browser()
        events = await scraper.discover_events()

        logger.info(f"\n发现 {len(events)} 个比赛事件:")
        for event in events[:10]:
            logger.info(f"  [{event.sport}] {event.competition}")
            logger.info(f"    {event.home_team} vs {event.away_team}")
            logger.info(f"    event_id: {event.event_id}")
            logger.info("")

        return events

    finally:
        await scraper.close_browser()


async def test_team_name_parsing():
    """测试队名解析逻辑"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 4: 队名解析逻辑")
    logger.info("=" * 60)

    config = PolymarketVenueConfig()
    scraper = PolymarketScraper(config, logger)

    test_cases = [
        "Lakers vs. Celtics",
        "Manchester United vs Chelsea",
        "Real Madrid vs Barcelona",
        "Team A vs Team B: Winner",
        "NBA: Lakers vs. Heat",
        "TeamAvs TeamB",  # 无空格
        "Invalid Title Without VS",
    ]

    logger.info("\n队名解析测试:")
    for title in test_cases:
        result = scraper._parse_team_names(title)
        if result:
            home, away = result
            logger.info(f"  ✓ '{title}'")
            logger.info(f"    → home: '{home}', away: '{away}'")
        else:
            logger.info(f"  ✗ '{title}' → 无法解析")


async def main():
    """主测试流程"""
    logger.info("Polymarket 市场发现测试开始")
    logger.info("=" * 60)

    # 测试队名解析（不需要网络）
    await test_team_name_parsing()

    # 询问是否继续网络测试
    print("\n" + "-" * 60)
    choice = input("是否继续网络测试？(y/n): ").strip().lower()

    if choice == "y":
        # 测试 Gamma API
        await test_gamma_api()

        # 测试 sport-competition 映射
        await test_sport_competition_mapping()

        # 测试完整发现流程
        await test_event_discovery()

    logger.info("\n" + "=" * 60)
    logger.info("测试完成")


if __name__ == "__main__":
    asyncio.run(main())
