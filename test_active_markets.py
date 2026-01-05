#!/usr/bin/env python3
"""
正确获取活跃市场
"""

from py_clob_client.client import ClobClient
import json


def test_active_markets():
    """测试获取活跃市场"""
    
    print('=' * 70)
    print('测试获取活跃市场')
    print('=' * 70)
    print()
    
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
    )
    
    # 获取所有市场
    response = client.get_markets()
    all_markets = response['data']
    
    print(f'总市场数: {len(all_markets)}')
    
    # 分析状态
    print('\n状态分析:')
    
    # 1. active=True, closed=False (真正活跃)
    truly_active = [m for m in all_markets if m.get('active') == True and m.get('closed') == False]
    print(f'1. active=True AND closed=False: {len(truly_active)}')
    
    # 2. active=True, closed=True (已关闭但仍标记为 active)
    active_but_closed = [m for m in all_markets if m.get('active') == True and m.get('closed') == True]
    print(f'2. active=True AND closed=True: {len(active_but_closed)}')
    
    # 3. active=False, closed=False
    inactive_open = [m for m in all_markets if m.get('active') == False and m.get('closed') == False]
    print(f'3. active=False AND closed=False: {len(inactive_open)}')
    
    # 4. active=False, closed=True
    inactive_closed = [m for m in all_markets if m.get('active') == False and m.get('closed') == True]
    print(f'4. active=False AND closed=True: {len(inactive_closed)}')
    
    print()
    
    # 查找真正活跃的体育市场
    sports_keywords = ['nfl', 'nba', 'mlb', 'nhl', 'soccer', 'basketball', 'vs', 'v ']
    
    active_sports = []
    for m in truly_active:
        question = m.get('question', '').lower()
        if any(kw in question for kw in sports_keywords):
            active_sports.append(m)
    
    print(f'真正活跃的体育市场: {len(active_sports)}')
    
    if active_sports:
        print('\n活跃体育市场示例:')
        for i, m in enumerate(active_sports[:10], 1):
            print(f'{i}. {m["question"][:80]}')
            print(f'   Active: {m["active"]}, Closed: {m["closed"]}')
        
        # 保存
        with open('polymarket_active_sports.json', 'w', encoding='utf-8') as f:
            json.dump(active_sports, f, indent=2, ensure_ascii=False)
        
        print(f'\n✅ 已保存到 polymarket_active_sports.json')
    else:
        print('\n⚠️  确实没有真正活跃的体育市场')
        
        # 查看真正活跃的市场是什么类型
        print('\n真正活跃的市场类型（前20个）:')
        for i, m in enumerate(truly_active[:20], 1):
            print(f'{i}. {m["question"][:80]}')


if __name__ == '__main__':
    test_active_markets()
