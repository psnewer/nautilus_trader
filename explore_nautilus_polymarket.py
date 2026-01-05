#!/usr/bin/env python3
"""
探索 NautilusTrader 的 Polymarket 集成
"""

import sys
from pathlib import Path


def find_polymarket_files():
    """查找 Polymarket 相关文件"""
    
    print('=' * 70)
    print('查找 NautilusTrader 中的 Polymarket 相关代码')
    print('=' * 70)
    print()
    
    # 查找文件
    nautilus_path = Path('nautilus_trader')
    
    if not nautilus_path.exists():
        print('❌ 未找到 nautilus_trader 目录')
        return
    
    # 搜索包含 polymarket 的文件
    polymarket_files = []
    
    for py_file in nautilus_path.rglob('*.py'):
        try:
            content = py_file.read_text(encoding='utf-8')
            if 'polymarket' in content.lower():
                polymarket_files.append(py_file)
        except:
            pass
    
    if polymarket_files:
        print(f'✅ 找到 {len(polymarket_files)} 个相关文件:')
        for f in polymarket_files:
            print(f'   {f}')
    else:
        print('❌ 未找到 Polymarket 相关文件')
    
    print()
    
    # 查看适配器目录
    print('=' * 70)
    print('NautilusTrader 适配器列表')
    print('=' * 70)
    print()
    
    adapters_path = nautilus_path / 'adapters'
    
    if adapters_path.exists():
        adapters = [d.name for d in adapters_path.iterdir() if d.is_dir() and not d.name.startswith('_')]
        print(f'找到 {len(adapters)} 个适配器:')
        for adapter in sorted(adapters):
            print(f'   - {adapter}')
    else:
        print('❌ 未找到 adapters 目录')
    
    print()
    
    # 检查是否有 Polymarket 适配器
    polymarket_adapter = adapters_path / 'polymarket' if adapters_path.exists() else None
    
    if polymarket_adapter and polymarket_adapter.exists():
        print('=' * 70)
        print('✅ 发现 Polymarket 适配器！')
        print('=' * 70)
        print()
        
        # 列出文件
        print('适配器文件:')
        for f in polymarket_adapter.rglob('*.py'):
            print(f'   {f.relative_to(polymarket_adapter)}')
        
        print()
        
        # 尝试导入并查看功能
        try:
            sys.path.insert(0, str(Path.cwd()))
            from nautilus_trader.adapters.polymarket import PolymarketDataClient
            
            print('=' * 70)
            print('PolymarketDataClient 方法:')
            print('=' * 70)
            
            methods = [m for m in dir(PolymarketDataClient) if not m.startswith('_')]
            for method in methods:
                print(f'   - {method}')
        
        except ImportError as e:
            print(f'⚠️  无法导入: {e}')
    
    else:
        print('=' * 70)
        print('❌ 未找到 Polymarket 适配器')
        print('=' * 70)
        print()
        print('建议:')
        print('   1. 检查 NautilusTrader 文档')
        print('   2. 查看是否有其他方式集成 Polymarket')
        print('   3. 或者使用我们自己开发的适配器')


if __name__ == '__main__':
    find_polymarket_files()
