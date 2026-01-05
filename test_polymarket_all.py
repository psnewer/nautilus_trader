#!/usr/bin/env python3
"""
测试获取所有 Polymarket 市场（包括已关闭的）
"""

from py_clob_client.client import ClobClient
import json


def test_all_markets():
    """获取所有市场"""
    
    print('=' * 70)
    print('获取所有 Polymarket 市场（包括已关闭）')
    print('=' * 70)
    print()
    
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
    )
    
    # 获取市场
    response = client.get_markets()
    markets = response['data']
    
    print(f'总市场数: {len(markets)}')
    print()
    
    # 统计
    active_count = sum(1 for m in markets if m.get('active', False))
    closed_count = sum(1 for m in markets if m.get('closed', False))
    
    print(f'活跃市场 (active=True): {active_count}')
    print(f'已关闭市场 (closed=True): {closed_count}')
    print()
    
    # 查找体育市场（包括已关闭的）
    sports_keywords = ['nfl', 'nba', 'mlb', 'nhl', 'soccer', 'basketball', 'vs', 'v ']
    
    sports_markets = []
    for m in markets:
        question = m.get('question', '').lower()
        if any(kw in question for kw in sports_keywords):
            sports_markets.append(m)
    
    print(f'体育相关市场: {len(sports_markets)}')
    print()
    
    # 显示一些体育市场
    print('示例体育市场（前10个）:')
    for i, m in enumerate(sports_markets[:10], 1):
        print(f'{i}. {m["question"][:80]}')
        print(f'   Active: {m.get("active")}, Closed: {m.get("closed")}')
    
    # 查找活跃的体育市场
    active_sports = [m for m in sports_markets if m.get('active', False) and not m.get('closed', False)]
    print(f'\n活跃的体育市场: {len(active_sports)}')
    
    if active_sports:
        print('\n活跃体育市场示例:')
        for i, m in enumerate(active_sports[:5], 1):
            print(f'{i}. {m["question"][:80]}')
    else:
        print('\n⚠️  没有活跃的体育市场')
        print('\n可能原因:')
        print('1. Polymarket 当前没有活跃的体育预测市场')
        print('2. 体育市场已经迁移到其他平台')
        print('3. 需要使用不同的 API 或参数')
    
    # 保存所有体育市场
    with open('polymarket_all_sports.json', 'w', encoding='utf-8') as f:
        json.dump(sports_markets[:50], f, indent=2, ensure_ascii=False)
    
    print(f'\n✅ 已保存前50个体育市场到 polymarket_all_sports.json')


if __name__ == '__main__':
    test_all_markets()
