#!/usr/bin/env python3
"""
检查 ClobClient.get_markets() 的实际返回格式
"""

from py_clob_client.client import ClobClient
import json


def inspect_markets():
    """检查市场数据格式"""
    
    print('=' * 70)
    print('检查 ClobClient.get_markets() 返回格式')
    print('=' * 70)
    print()
    
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
    )
    
    print('调用 get_markets()...')
    markets = client.get_markets()
    
    print(f'返回类型: {type(markets)}')
    print(f'长度: {len(markets) if isinstance(markets, (list, dict)) else "N/A"}')
    print()
    
    if isinstance(markets, list):
        print('✅ 返回的是列表')
        if markets:
            print(f'\n第一个元素类型: {type(markets[0])}')
            print(f'\n第一个元素内容:')
            print(json.dumps(markets[0], indent=2)[:500])
    
    elif isinstance(markets, dict):
        print('✅ 返回的是字典')
        print(f'\n字典键: {list(markets.keys())}')
        
        # 检查是否有 data 字段
        if 'data' in markets:
            print(f'\ndata 字段类型: {type(markets["data"])}')
            if isinstance(markets['data'], list):
                print(f'data 列表长度: {len(markets["data"])}')
                if markets['data']:
                    print(f'\n第一个市场:')
                    print(json.dumps(markets['data'][0], indent=2)[:500])
    
    else:
        print(f'⚠️  未知类型: {type(markets)}')
        print(f'内容: {str(markets)[:200]}')
    
    # 保存原始响应
    with open('clob_raw_response.json', 'w', encoding='utf-8') as f:
        json.dump(markets, f, indent=2, ensure_ascii=False)
    
    print(f'\n✅ 原始响应已保存到 clob_raw_response.json')


if __name__ == '__main__':
    inspect_markets()
