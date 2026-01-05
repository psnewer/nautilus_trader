#!/usr/bin/env python3
"""
检查 Polymarket API 文档
"""

import requests


def check_api_docs():
    """检查 API 文档"""
    
    print('检查 Polymarket API 文档')
    print('=' * 70)
    print()
    
    docs_urls = [
        'https://docs.polymarket.com/api-reference',
        'https://gamma-api.polymarket.com/docs',
        'https://gamma-api.polymarket.com/swagger',
        'https://gamma-api.polymarket.com/openapi.json',
    ]
    
    for url in docs_urls:
        try:
            response = requests.get(url, timeout=10)
            print(f'{url}')
            print(f'   状态码: {response.status_code}')
            
            if response.status_code == 200:
                content_type = response.headers.get('content-type', '')
                print(f'   Content-Type: {content_type}')
                
                if 'json' in content_type:
                    print('   找到 JSON 文档')
                    filename = url.split('/')[-1] + '.json'
                    with open(filename, 'w') as f:
                        f.write(response.text)
                    print(f'   保存到: {filename}')
            
            print()
        
        except Exception as e:
            print(f'   错误: {e}\n')


if __name__ == '__main__':
    check_api_docs()
