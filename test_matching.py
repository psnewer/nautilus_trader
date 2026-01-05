"""测试匹配逻辑"""

# 模拟数据
web_competition_to_sport = {'ATP': 'Tennis'}
api_competition = 'ATP'

# 测试直接匹配
print('测试 1: 直接匹配')
for web_comp in web_competition_to_sport.keys():
    if api_competition.lower() == web_comp.lower():
        print(f'✅ 匹配成功: {web_comp}')
        break
else:
    print('❌ 没有匹配')

# 测试字典查找
print('\n测试 2: 字典查找')
if api_competition in web_competition_to_sport:
    print(f'✅ 找到: {web_competition_to_sport[api_competition]}')
else:
    print('❌ 没找到')

# 检查实际代码中的问题
print('\n测试 3: 实际代码逻辑')

web_competition = None

# 先直接匹配
for web_comp in web_competition_to_sport.keys():
    if api_competition.lower() == web_comp.lower():
        web_competition = web_comp
        break

if web_competition:
    sport = web_competition_to_sport.get(web_competition, 'Unknown')
    print(f'✅ web_competition: {web_competition}')
    print(f'✅ sport: {sport}')
else:
    print('❌ web_competition 为 None')
