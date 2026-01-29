#!/usr/bin/env python
"""
测试市场发现功能

直接调用 discovery 逻辑，用于调试。
"""

import asyncio
import logging
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


async def test_discovery():
    """测试市场发现"""
    # 配置日志
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    print("=" * 70)
    print("测试市场发现")
    print("=" * 70)

    from src.arbitrage.services.web_gateway.state import app_state
    from src.arbitrage.services.web_gateway.routes.discovery import _run_discovery

    # 显示配置
    config = app_state.discovery_config_obj
    print(f"\nPolymarket 配置:")
    print(f"  Enabled: {config.venues.polymarket.enabled}")
    print(f"  Sports: {len(config.venues.polymarket.sports)}")
    for sport in config.venues.polymarket.sports:
        print(f"    - {sport.sport}: {sport.competitions}")

    print(f"\nOrbitExch 配置:")
    print(f"  Enabled: {config.venues.orbitexch.enabled}")
    print(f"  Sports: {len(config.venues.orbitexch.sports)}")
    for sport in config.venues.orbitexch.sports:
        print(f"    - {sport.sport}: {sport.competitions}")

    print("\n" + "=" * 70)
    print("开始执行市场发现...")
    print("=" * 70 + "\n")

    # 运行发现
    await _run_discovery(venue=None)

    print("\n" + "=" * 70)
    print("发现结果:")
    print("=" * 70)

    results = app_state.get_discovery_results()
    print(f"Polymarket: {results['summary']['polymarket_count']} events")
    print(f"OrbitExch: {results['summary']['orbitexch_count']} events")


if __name__ == "__main__":
    asyncio.run(test_discovery())
