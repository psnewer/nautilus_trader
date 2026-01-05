"""
探索 Polymarket API 结构
"""

import subprocess
import json


def curl_get(url):
    """使用 curl 获取数据"""
    cmd = [
        'curl', '-s',
        '-H', 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        '-H', 'Accept: application/json',
        url
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    
    if result.returncode == 0:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            print(f'❌ JSON 解析失败: {e}')
            print(f'响应内容前500字符:')
            print(result.stdout[:500])
            print(f'\n响应内容类型: {type(result.stdout)}')
            return None
    else:
        print(f'❌ 请求失败: {result.stderr}')
        return None


def explore_clob_api():
    """探索 CLOB API"""
    
    print('=' * 70)
    print('探索 Polymarket CLOB API')
    print('=' * 70)
    print()
    
    # 1. 获取市场列表
    print('1️⃣  获取市场列表...')
    url = 'https://clob.polymarket.com/markets'
    data = curl_get(url)
    
    if data is None:
        print('❌ 无法获取数据，停止探索')
        return
    
    print(f'✅ 成功获取数据')
    print(f'数据类型: {type(data)}')
    
    if isinstance(data, list):
        print(f'列表长度: {len(data)}')
        
        if data:
            print('\n第一个元素的类型:', type(data[0]))
            
            # 检查元素是否是字典
            if isinstance(data[0], dict):
                print('\n第一个市场的结构:')
                first_market = data[0]
                print(json.dumps(first_market, indent=2)[:1000])
                
                print('\n所有字段:')
                for key in sorted(first_market.keys()):
                    value = first_market[key]
                    value_type = type(value).__name__
                    
                    if isinstance(value, (dict, list)):
                        value_preview = f'{value_type} (length: {len(value)})'
                    else:
                        value_preview = str(value)[:50]
                    
                    print(f'  {key:25s} : {value_type:10s} = {value_preview}')
                
                # 保存完整数据
                with open('polymarket_clob_sample.json', 'w', encoding='utf-8') as f:
                    json.dump(data[:10], f, indent=2, ensure_ascii=False)
                print('\n✅ 前10个市场已保存到 polymarket_clob_sample.json')
            
            elif isinstance(data[0], str):
                print('⚠️  列表元素是字符串，不是字典')
                print(f'前3个元素: {data[:3]}')
            
            else:
                print(f'⚠️  未知的元素类型: {type(data[0])}')
    
    elif isinstance(data, dict):
        print('字典键:', list(data.keys())[:20])
        
        # 尝试找到市场数据
        for key in ['data', 'markets', 'results', 'items']:
            if key in data:
                print(f'\n找到可能的市场列表键: {key}')
                market_list = data[key]
                print(f'类型: {type(market_list)}')
                if isinstance(market_list, list):
                    print(f'长度: {len(market_list)}')
                    if market_list and isinstance(market_list[0], dict):
                        print(f'第一个市场的字段: {list(market_list[0].keys())[:10]}')
    
    else:
        print(f'⚠️  未知的数据类型: {type(data)}')


def test_different_endpoints():
    """测试不同的 API 端点"""
    
    print('\n' + '=' * 70)
    print('测试不同的 API 端点')
    print('=' * 70)
    print()
    
    endpoints = [
        ('CLOB Markets', 'https://clob.polymarket.com/markets'),
        ('CLOB Markets with limit', 'https://clob.polymarket.com/markets?limit=5'),
        ('Gamma Markets', 'https://gamma-api.polymarket.com/markets?limit=5'),
        ('Strapi Markets', 'https://strapi-matic.poly.market/markets?_limit=5'),
    ]
    
    for name, url in endpoints:
        print(f'\n{name}')
        print(f'URL: {url}')
        print('-' * 70)
        
        data = curl_get(url)
        
        if data:
            print(f'✅ 成功')
            print(f'类型: {type(data)}')
            
            if isinstance(data, list):
                print(f'长度: {len(data)}')
                if data:
                    print(f'第一个元素类型: {type(data[0])}')
                    if isinstance(data[0], dict):
                        print(f'字段数: {len(data[0])}')
                        print(f'部分字段: {list(data[0].keys())[:5]}')
            
            elif isinstance(data, dict):
                print(f'键: {list(data.keys())[:10]}')
        else:
            print('❌ 失败')


def save_raw_response():
    """保存原始响应以便检查"""
    
    print('\n' + '=' * 70)
    print('保存原始响应')
    print('=' * 70)
    print()
    
    url = 'https://clob.polymarket.com/markets?limit=3'
    
    cmd = ['curl', '-s', url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    
    if result.returncode == 0:
        # 保存原始响应
        with open('polymarket_raw_response.txt', 'w', encoding='utf-8') as f:
            f.write(result.stdout)
        
        print('✅ 原始响应已保存到 polymarket_raw_response.txt')
        print(f'\n前500字符:')
        print(result.stdout[:500])
        
        # 尝试美化显示
        try:
            data = json.loads(result.stdout)
            with open('polymarket_formatted_response.json', 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print('\n✅ 格式化后已保存到 polymarket_formatted_response.json')
        except:
            print('\n⚠️  无法格式化为 JSON')


if __name__ == '__main__':
    save_raw_response()
    print('\n')
    explore_clob_api()
    print('\n')
    test_different_endpoints()
    
    print('\n' + '=' * 70)
    print('探索完成！')
    print('=' * 70)
    print('\n请查看生成的文件:')
    print('  - polymarket_raw_response.txt (原始响应)')
    print('  - polymarket_formatted_response.json (格式化的 JSON)')
    print('  - polymarket_clob_sample.json (样本数据)')
