#!/usr/bin/env python3
"""
探索 Polymarket 官方 API

根据文档: https://docs.polymarket.com/
"""

import requests
import json


def explore_gamma_api():
    """
    探索 Gamma API
    
    根据文档，Gamma API 是主要的市场数据 API
    """
    
    print('=' * 70)
    print('探索 Gamma API (官方推荐)')
    print('=' * 70)
    print()
    
    # Gamma API 端点
    base_url = 'https://gamma-api.polymarket.com'
    
    # 1. 获取所有市场
    print('1. GET /markets - 获取所有市场')
    print('-' * 70)
    
    try:
        response = requests.get(f'{base_url}/markets', timeout=10)
        print(f'状态码: {response.status_code}')
        
        if response.status_code == 200:
            data = response.json()
            
            if isinstance(data, list):
                print(f'✅ 返回列表，长度: {len(data)}')
                
                if data:
                    print('\n第一个市场的字段:')
                    for key in sorted(data[0].keys()):
                        print(f'   - {key}')
                    
                    # 保存样本
                    with open('gamma_markets_sample.json', 'w') as f:
                        json.dump(data[:5], f, indent=2)
                    print('\n✅ 保存前5个到 gamma_markets_sample.json')
            
            elif isinstance(data, dict):
                print(f'✅ 返回字典，键: {list(data.keys())}')
        else:
            print(f'❌ 失败: {response.text[:200]}')
    
    except Exception as e:
        print(f'❌ 错误: {e}')
    
    print()
    
    # 2. 查询参数
    print('2. GET /markets 带参数')
    print('-' * 70)
    
    params_to_test = [
        {'limit': 10},
        {'closed': 'false'},
        {'archived': 'false'},
        {'active': 'true'},
        {'limit': 10, 'closed': 'false'},
    ]
    
    for params in params_to_test:
        try:
            response = requests.get(f'{base_url}/markets', params=params, timeout=10)
            print(f'参数: {params}')
            print(f'   状态码: {response.status_code}')
            
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    print(f'   结果数: {len(data)}')
                    if data:
                        print(f'   第一个: {data[0].get("question", "N/A")[:60]}')
            print()
        
        except Exception as e:
            print(f'   ❌ 错误: {e}\n')
    
    # 3. 检查特定端点
    print('3. 其他端点')
    print('-' * 70)
    
    endpoints = [
        '/events',
        '/markets/active',
        '/sports',
        '/tags',
    ]
    
    for endpoint in endpoints:
        try:
            response = requests.get(f'{base_url}{endpoint}', timeout=10)
            print(f'{endpoint}: {response.status_code}')
        except Exception as e:
            print(f'{endpoint}: ❌ {e}')


def explore_clob_api():
    """
    探索 CLOB API
    
    用于交易订单
    """
    
    print('\n' + '=' * 70)
    print('探索 CLOB API (订单簿)')
    print('=' * 70)
    print()
    
    base_url = 'https://clob.polymarket.com'
    
    # 测试端点
    endpoints = [
        '/markets',
        '/markets?closed=false',
        '/markets?active=true',
        '/markets?limit=10&closed=false&active=true',
    ]
    
    for endpoint in endpoints:
        try:
            url = f'{base_url}{endpoint}'
            response = requests.get(url, timeout=10)
            
            print(f'{endpoint}')
            print(f'   状态码: {response.status_code}')
            
            if response.status_code == 200:
                data = response.json()
                
                # 检查数据结构
                if isinstance(data, dict):
                    print(f'   类型: dict')
                    print(f'   键: {list(data.keys())}')
                    
                    if 'data' in data:
                        markets = data['data']
                        print(f'   市场数: {len(markets)}')
                        
                        # 统计状态
                        if markets:
                            active_count = sum(1 for m in markets if m.get('active'))
                            closed_count = sum(1 for m in markets if m.get('closed'))
                            print(f'   active=True: {active_count}')
                            print(f'   closed=True: {closed_count}')
                            print(f'   closed=False: {len(markets) - closed_count}')
            
            print()
        
        except Exception as e:
            print(f'   ❌ 错误: {e}\n')


def check_documentation():
    """检查文档链接"""
    
    print('\n' + '=' * 70)
    print('Polymarket API 文档')
    print('=' * 70)
    print()
    
    print('官方文档:')
    print('   主页: https://docs.polymarket.com/')
    print('   快速开始: https://docs.polymarket.com/quickstart/overview')
    print('   API 参考: https://docs.polymarket.com/api-reference')
    print()
    
    print('主要 API:')
    print('   1. Gamma API - 市场数据')
    print('      Base URL: https://gamma-api.polymarket.com')
    print('      用途: 获取市场信息、价格、统计')
    print()
    print('   2. CLOB API - 订单簿')
    print('      Base URL: https://clob.polymarket.com')
    print('      用途: 订单管理、交易')
    print()
    print('   3. Strapi API - CMS')
    print('      Base URL: https://strapi-matic.poly.market')
    print('      用途: 编辑内容、元数据')


if __name__ == '__main__':
    check_documentation()
    print('\n')
    explore_gamma_api()
    print('\n')
    explore_clob_api()
