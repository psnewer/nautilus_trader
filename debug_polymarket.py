"""调试 Polymarket 解析"""

import requests
import json

# 测试获取 ATP 事件
response = requests.get(
    'https://gamma-api.polymarket.com/events',
    params={'tag_id': '864', 'closed': 'false', 'limit': 10},
    timeout=30
)

data = response.json()

print(f'返回 {len(data)} 个事件\n')

for i, item in enumerate(data[:3], 1):
    print(f'事件 {i}:')
    print(f'  title: {item.get("title")}')
    
    # 检查 series
    series = item.get('series', [])
    print(f'  series 类型: {type(series)}')
    print(f'  series 长度: {len(series)}')
    
    if series:
        print(f'  series[0]: {series[0].get("title") if isinstance(series[0], dict) else series[0]}')
    
    # 检查是否有 vs
    title = item.get('title', '')
    print(f'  包含 "vs.": {" vs. " in title}')
    print(f'  包含 "vs": {" vs " in title}')
    
    # 尝试拆分
    if ' vs. ' in title:
        parts = title.split(' vs. ', 1)
        print(f'  拆分 (vs.): {parts}')
    elif ' vs ' in title:
        parts = title.split(' vs ', 1)
        print(f'  拆分 (vs): {parts}')
    else:
        print(f'  ❌ 无法拆分')
    
    print()

