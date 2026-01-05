"""完整调试流程"""

import requests
import json
import re
from bs4 import BeautifulSoup

print('步骤 1: 获取网页映射')
print('=' * 70)

response = requests.get('https://polymarket.com/sports', timeout=30)
soup = BeautifulSoup(response.text, 'html.parser')
sport_items = soup.find_all('div', class_='group/sports-item')

web_competition_to_sport = {}

for item in sport_items:
    child_divs = item.find_all('div', recursive=False)
    
    if len(child_divs) < 2:
        continue
    
    sport_p = child_divs[0].find('p')
    if not sport_p:
        continue
    
    sport_name = sport_p.get_text(strip=True)
    comp_links = child_divs[1].find_all('a', class_='block')
    
    for link in comp_links:
        comp_p = link.find('p')
        if comp_p:
            comp_name = comp_p.get_text(strip=True)
            web_competition_to_sport[comp_name] = sport_name

print(f'获取 {len(web_competition_to_sport)} 个映射')
print('\n网页 competitions (前 20):')
for i, comp in enumerate(list(web_competition_to_sport.keys())[:20], 1):
    print(f'{i}. {comp} -> {web_competition_to_sport[comp]}')

print('\n步骤 2: 获取 API 事件 (tag_id=864)')
print('=' * 70)

response = requests.get(
    'https://gamma-api.polymarket.com/events',
    params={'tag_id': '864', 'closed': 'false', 'limit': 100},
    timeout=30
)

events = response.json()
print(f'获取 {len(events)} 个事件')

# 筛选有 vs 的事件
vs_events = []
for event in events:
    title = event.get('title', '')
    if re.search(r'\s*vs\.?\s*', title, re.IGNORECASE):
        vs_events.append(event)

print(f'其中有 vs 的事件: {len(vs_events)}')

if vs_events:
    print('\n有 vs 的事件示例:')
    for i, event in enumerate(vs_events[:3], 1):
        title = event.get('title', '')
        series = event.get('series', [])
        
        print(f'\n{i}. {title}')
        print(f'   series: {series}')
        
        # 提取 competition
        if series and len(series) > 0:
            api_comp = series[0].get('title', '') if isinstance(series[0], dict) else ''
            print(f'   API competition: {api_comp}')
            
            # 尝试匹配
            if api_comp.lower() in [c.lower() for c in web_competition_to_sport.keys()]:
                print(f'   ✅ 直接匹配成功')
            else:
                print(f'   ⚠️  需要相似度匹配')
                # 显示最相似的
                for web_comp in list(web_competition_to_sport.keys())[:5]:
                    print(f'      与 "{web_comp}" 比较')
        else:
            print(f'   ❌ series 为空')

