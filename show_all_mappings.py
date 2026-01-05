"""显示所有映射"""

import requests
from bs4 import BeautifulSoup

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

print(f'所有 {len(web_competition_to_sport)} 个映射:\n')

# 按 sport 分组
by_sport = {}
for comp, sport in web_competition_to_sport.items():
    if sport not in by_sport:
        by_sport[sport] = []
    by_sport[sport].append(comp)

for sport, comps in sorted(by_sport.items()):
    print(f'\n{sport}:')
    for comp in sorted(comps):
        print(f'  - {comp}')
