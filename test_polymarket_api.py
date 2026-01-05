"""
测试 Polymarket API 的多个端点
"""

import asyncio
import aiohttp
import json


async def test_api_endpoints():
    """测试不同的 API 端点"""
    
    endpoints = [
        # CLOB API
        {
            'name': 'CLOB Markets',
            'url': 'https://clob.polymarket.com/markets',
            'params': {}
        },
        # Gamma API
        {
            'name': 'Gamma Markets',
            'url': 'https://gamma-api.polymarket.com/markets',
            'params': {'limit': 10}
        },
        # Strapi API
        {
            'name': 'Strapi Events',
            'url': 'https://strapi-matic.poly.market/markets',
            'params': {'_limit': 10}
        },
    ]
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'application/json',
    }
    
    timeout = aiohttp.ClientTimeout(total=30)
    
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for endpoint in endpoints:
            print('=' * 70)
            print(f'测试: {endpoint["name"]}')
            print(f'URL: {endpoint["url"]}')
            print('=' * 70)
            
            try:
                async with session.get(endpoint['url'], params=endpoint['params']) as response:
                    print(f'状态码: {response.status}')
                    
                    if response.status == 200:
                        data = await response.json()
                        
                        # 显示结构
                        print(f'响应类型: {type(data)}')
                        
                        if isinstance(data, list):
                            print(f'列表长度: {len(data)}')
                            if data:
                                print(f'第一项键: {data[0].keys() if isinstance(data[0], dict) else type(data[0])}')
                        elif isinstance(data, dict):
                            print(f'字典键: {data.keys()}')
                        
                        # 保存样本
                        filename = f'polymarket_{endpoint["name"].replace(" ", "_").lower()}.json'
                        with open(filename, 'w', encoding='utf-8') as f:
                            json.dump(data, f, ensure_ascii=False, indent=2)
                        print(f'✅ 样本已保存: {filename}')
                    else:
                        text = await response.text()
                        print(f'❌ 错误: {text[:200]}')
            
            except Exception as e:
                print(f'❌ 请求失败: {e}')
            
            print()


if __name__ == '__main__':
    asyncio.run(test_api_endpoints())
