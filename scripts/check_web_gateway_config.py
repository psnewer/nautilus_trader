#!/usr/bin/env python
"""
检查 Web Gateway 配置

用法:
    python scripts/check_web_gateway_config.py
"""

import json
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def main():
    from src.arbitrage.services.web_gateway.state import app_state

    print("=" * 70)
    print("Web Gateway 配置检查")
    print("=" * 70)

    # 检查 Discovery 配置
    print("\n[Discovery 配置]")
    discovery_config = app_state.get_discovery_config()
    print(f"  Enabled: {discovery_config['enabled']}")
    print(f"  Poll Interval: {discovery_config['poll_interval_sec']}s")

    # Polymarket
    poly_config = discovery_config["venues"]["polymarket"]
    print(f"\n  Polymarket:")
    print(f"    Enabled: {poly_config['enabled']}")
    print(f"    Sports: {len(poly_config['sports'])} configured")
    for sport in poly_config["sports"]:
        print(f"      - {sport['sport']}: {len(sport['competitions'])} competitions")
        for comp in sport["competitions"]:
            print(f"          * {comp}")

    # OrbitExch
    orbit_config = discovery_config["venues"]["orbitexch"]
    print(f"\n  OrbitExch:")
    print(f"    Enabled: {orbit_config['enabled']}")
    print(f"    Sports: {len(orbit_config['sports'])} configured")
    for sport in orbit_config["sports"]:
        print(f"      - {sport['sport']}: {len(sport['competitions'])} competitions")
        for comp in sport["competitions"]:
            print(f"          * {comp}")

    # 检查 Matching 配置
    print("\n[Matching 配置]")
    matching_config = app_state.get_matching_config()
    print(f"  Enabled: {matching_config['enabled']}")
    print(f"  Min Similarity: {matching_config['min_similarity']}")
    print(f"  Sport Aliases: {len(matching_config['sport_aliases'])} defined")
    print(f"  Competition Aliases: {len(matching_config['competition_aliases'])} defined")

    # 检查数据状态
    print("\n[数据状态]")
    print(f"  Polymarket Events: {len(app_state.polymarket_events)}")
    print(f"  OrbitExch Events: {len(app_state.orbitexch_events)}")
    print(f"  Matched Pairs: {len(app_state._matched_pairs)}")

    # 警告检查
    print("\n[诊断]")
    warnings = []

    if not poly_config["enabled"] and not orbit_config["enabled"]:
        warnings.append("⚠️  两个平台都未启用")

    if poly_config["enabled"] and not poly_config["sports"]:
        warnings.append("⚠️  Polymarket 已启用但未配置 sports")

    if orbit_config["enabled"] and not orbit_config["sports"]:
        warnings.append("⚠️  OrbitExch 已启用但未配置 sports")

    if warnings:
        for warning in warnings:
            print(f"  {warning}")
    else:
        print("  ✓ 配置正常")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
