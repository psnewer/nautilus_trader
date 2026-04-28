"""
测试 Polymarket closed 事件过滤

验证 closed=true 的事件是否被正确过滤
"""

import asyncio
import logging
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.arbitrage.services.market_discovery.config import (
    PolymarketVenueConfig,
    SportConfig,
)
from nautilus_trader.adapters.polymarket.scraper import (
    PolymarketScraper,
)


async def main():
    # 配置日志
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # 配置 Polymarket scraper - 只爬取 tennis/atp
    config = PolymarketVenueConfig(
        enabled=True,
        sports=[
            SportConfig(sport="Tennis", competitions=["atp"]),
        ],
    )

    scraper = PolymarketScraper(config)

    try:
        print("开始测试 Polymarket closed 过滤...")
        print("目标: Tennis/atp")
        print("=" * 80)

        # 直接测试 series 10365（用户提供的例子）
        print("\n测试 series 10365:")
        series_events = await scraper.fetch_series_events("10365")
        print(f"返回 {len(series_events)} 个 events")

        closed_count = 0
        inactive_count = 0
        valid_count = 0

        for event in series_events:
            event_id = event.get("id")
            title = event.get("title", "")
            closed = event.get("closed", False)
            active = event.get("active", False)

            status = f"closed={closed}, active={active}"

            if closed:
                closed_count += 1
                print(f"  ❌ CLOSED:   {event_id} - {title[:50]} ({status})")
            elif not active:
                inactive_count += 1
                print(f"  ⚠️  INACTIVE: {event_id} - {title[:50]} ({status})")
            else:
                valid_count += 1
                print(f"  ✅ VALID:    {event_id} - {title[:50]} ({status})")

        print(f"\n统计: {valid_count} valid (closed=false, active=true), {closed_count} closed, {inactive_count} inactive")
        print("=" * 80)

        # 运行完整的 discover_events
        print("\n运行完整 discover_events:")
        events = await scraper.discover_events(
            target_sports=["Tennis"],
            target_competitions=["atp"],
        )

        print(f"\n最终发现 {len(events)} 个事件")
        for e in events:
            print(f"  - {e.home_team} vs {e.away_team} (ID: {e.event_id})")

    finally:
        await scraper.close_browser()


if __name__ == "__main__":
    asyncio.run(main())
