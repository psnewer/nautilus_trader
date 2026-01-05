#!/usr/bin/env python3
"""
检查 NautilusTrader Polymarket 适配器的实际结构
"""

from pathlib import Path
import importlib.util


def inspect_polymarket_adapter():
    """检查适配器结构"""
    
    print('=' * 70)
    print('检查 NautilusTrader Polymarket 适配器')
    print('=' * 70)
    print()
    
    # 查找适配器目录
    adapter_path = Path('nautilus_trader/adapters/polymarket')
    
    if not adapter_path.exists():
        print('❌ 未找到 Polymarket 适配器目录')
        return
    
    print(f'✅ 找到适配器目录: {adapter_path}')
    print()
    
    # 列出所有文件
    print('目录内容:')
    for item in sorted(adapter_path.rglob('*')):
        if item.is_file():
            rel_path = item.relative_to(adapter_path)
            size = item.stat().st_size
            print(f'   {rel_path} ({size} bytes)')
    
    print()
    
    # 查看 __init__.py
    init_file = adapter_path / '__init__.py'
    if init_file.exists():
        print('=' * 70)
        print('__init__.py 内容:')
        print('=' * 70)
        content = init_file.read_text()
        print(content)
        print()
    
    # 尝试导入模块
    print('=' * 70)
    print('尝试导入模块:')
    print('=' * 70)
    
    try:
        import nautilus_trader.adapters.polymarket as pm
        print('✅ 成功导入 nautilus_trader.adapters.polymarket')
        print()
        print('可用的内容:')
        for name in dir(pm):
            if not name.startswith('_'):
                obj = getattr(pm, name)
                obj_type = type(obj).__name__
                print(f'   - {name} ({obj_type})')
    except Exception as e:
        print(f'❌ 导入失败: {e}')
    
    print()
    
    # 查找所有 Python 文件并尝试导入
    print('=' * 70)
    print('查找所有模块:')
    print('=' * 70)
    
    for py_file in adapter_path.glob('*.py'):
        if py_file.name.startswith('_'):
            continue
        
        module_name = py_file.stem
        print(f'\n{module_name}.py:')
        
        try:
            # 读取文件查找类定义
            content = py_file.read_text()
            
            # 查找类定义
            import re
            classes = re.findall(r'^class (\w+)', content, re.MULTILINE)
            if classes:
                print(f'   类: {", ".join(classes)}')
            
            # 查找函数定义
            functions = re.findall(r'^def (\w+)', content, re.MULTILINE)
            if functions:
                print(f'   函数: {", ".join(functions[:5])}...' if len(functions) > 5 else f'   函数: {", ".join(functions)}')
        
        except Exception as e:
            print(f'   ❌ 读取失败: {e}')


if __name__ == '__main__':
    inspect_polymarket_adapter()
