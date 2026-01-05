"""测试解析单个事件"""

event_data = {
    'id': '140070',
    'title': 'Brisbane International: Daniil Medvedev vs Marton Fucsovics',
    'series': [{
        'title': 'ATP',
    }]
}

web_competition_to_sport = {'ATP': 'Tennis'}

# 提取 teams
import re
title = event_data['title']

match = re.split(r'\s*vs\.?\s*', title, maxsplit=1, flags=re.IGNORECASE)
if len(match) == 2:
    home = match[0].strip()
    away = match[1].strip()
    
    if ':' in home:
        home = home.split(':')[-1].strip()
    
    print(f'✅ 提取球队成功:')
    print(f'   Home: {home}')
    print(f'   Away: {away}')
else:
    print('❌ 无法提取球队')
    exit()

# 提取 competition
series = event_data.get('series', [])
if series and len(series) > 0:
    api_competition = series[0].get('title', '')
    print(f'✅ API competition: {api_competition}')
else:
    print('❌ series 为空')
    exit()

# 匹配 web competition
web_competition = None

for web_comp in web_competition_to_sport.keys():
    if api_competition.lower() == web_comp.lower():
        web_competition = web_comp
        break

if web_competition:
    print(f'✅ Web competition: {web_competition}')
    sport = web_competition_to_sport.get(web_competition)
    print(f'✅ Sport: {sport}')
else:
    print('❌ 未匹配到 web competition')

