#!/usr/bin/env python
"""
Web Gateway 配置管理工具

用法:
    python scripts/manage_web_config.py backup      # 备份当前配置
    python scripts/manage_web_config.py restore     # 恢复默认配置
    python scripts/manage_web_config.py show        # 显示当前配置
"""

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

CONFIG_FILE = project_root / "src/arbitrage/services/web_gateway/default_config.json"
BACKUP_DIR = project_root / "src/arbitrage/services/web_gateway/backups"


def backup_config():
    """备份当前配置"""
    if not CONFIG_FILE.exists():
        print("❌ 配置文件不存在")
        return False

    # 创建备份目录
    BACKUP_DIR.mkdir(exist_ok=True)

    # 生成备份文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = BACKUP_DIR / f"config_backup_{timestamp}.json"

    # 复制文件
    shutil.copy2(CONFIG_FILE, backup_file)
    print(f"✓ 配置已备份到: {backup_file}")
    return True


def restore_default_config():
    """恢复默认配置"""
    default_config = {
        "discovery": {
            "enabled": True,
            "poll_interval_sec": 60,
            "venues": {
                "polymarket": {
                    "enabled": True,
                    "sports": [
                        {
                            "sport": "Soccer",
                            "competitions": ["epl", "lal", "sea", "bl1", "fl1", "ucl"]
                        },
                        {
                            "sport": "Tennis",
                            "competitions": ["atp", "wta"]
                        },
                        {
                            "sport": "Basketball",
                            "competitions": ["nba", "ncaab"]
                        }
                    ]
                },
                "orbitexch": {
                    "enabled": True,
                    "sports": [
                        {
                            "sport": "Soccer",
                            "competitions": [
                                "English Premier League",
                                "Spanish La Liga",
                                "Italian Serie A",
                                "German Bundesliga",
                                "French Ligue 1",
                                "UEFA Champions League"
                            ]
                        },
                        {
                            "sport": "Tennis",
                            "competitions": ["ATP", "WTA"]
                        },
                        {
                            "sport": "Basketball",
                            "competitions": ["NBA"]
                        }
                    ]
                }
            }
        },
        "matching": {
            "enabled": True,
            "min_similarity": 1,
            "sport_aliases": {
                "Football": "Soccer",
                "football": "Soccer",
                "FOOTBALL": "Soccer",
                "soccer": "Soccer",
                "SOCCER": "Soccer",
                "basketball": "Basketball",
                "BASKETBALL": "Basketball",
                "tennis": "Tennis",
                "TENNIS": "Tennis"
            },
            "competition_aliases": {
                "epl": "English Premier League",
                "EPL": "English Premier League",
                "Premier League": "English Premier League",
                "English Premier League": "English Premier League",
                "lal": "Spanish La Liga",
                "La Liga": "Spanish La Liga",
                "LaLiga": "Spanish La Liga",
                "Spanish La Liga": "Spanish La Liga",
                "sea": "Italian Serie A",
                "SEA": "Italian Serie A",
                "Serie A": "Italian Serie A",
                "Italian Serie A": "Italian Serie A",
                "bl1": "German Bundesliga",
                "Bundesliga": "German Bundesliga",
                "German Bundesliga": "German Bundesliga",
                "fl1": "French Ligue 1",
                "Ligue 1": "French Ligue 1",
                "French Ligue 1": "French Ligue 1",
                "ucl": "UEFA Champions League",
                "UCL": "UEFA Champions League",
                "Champions League": "UEFA Champions League",
                "UEFA Champions League": "UEFA Champions League",
                "atp": "ATP",
                "ATP": "ATP",
                "wta": "WTA",
                "WTA": "WTA",
                "nba": "NBA",
                "NBA": "NBA",
                "ncaab": "NCAA Basketball",
                "NCAAB": "NCAA Basketball"
            }
        }
    }

    # 先备份当前配置
    if CONFIG_FILE.exists():
        print("备份当前配置...")
        backup_config()

    # 写入默认配置
    with open(CONFIG_FILE, 'w') as f:
        json.dump(default_config, f, indent=2)

    print(f"✓ 默认配置已恢复")
    return True


def show_config():
    """显示当前配置"""
    if not CONFIG_FILE.exists():
        print("❌ 配置文件不存在")
        return False

    with open(CONFIG_FILE) as f:
        config = json.load(f)

    print("=" * 70)
    print("当前配置")
    print("=" * 70)

    # Discovery
    print("\n[Discovery - Polymarket]")
    for sport in config['discovery']['venues']['polymarket']['sports']:
        print(f"  {sport['sport']}: {sport['competitions']}")

    print("\n[Discovery - OrbitExch]")
    for sport in config['discovery']['venues']['orbitexch']['sports']:
        print(f"  {sport['sport']}: {sport['competitions']}")

    # Matching
    print("\n[Matching]")
    print(f"  Min Similarity: {config['matching']['min_similarity']}")
    print(f"  Sport Aliases: {len(config['matching']['sport_aliases'])} 个")
    print(f"  Competition Aliases: {len(config['matching']['competition_aliases'])} 个")

    print("\n" + "=" * 70)
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Web Gateway 配置管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "action",
        choices=["backup", "restore", "show"],
        help="操作：backup=备份配置, restore=恢复默认, show=显示配置",
    )

    args = parser.parse_args()

    if args.action == "backup":
        backup_config()
    elif args.action == "restore":
        restore_default_config()
    elif args.action == "show":
        show_config()


if __name__ == "__main__":
    main()
