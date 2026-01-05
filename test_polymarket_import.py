#!/usr/bin/env python3
"""
测试不同的导入方式
"""

print('测试 Polymarket 适配器导入')
print('=' * 70)
print()

# 方式 1: 直接导入模块
print('1. 导入模块')
try:
    import nautilus_trader.adapters.polymarket as pm
    print('   ✅ 成功')
    print(f'   可用内容: {[x for x in dir(pm) if not x.startswith("_")]}')
except Exception as e:
    print(f'   ❌ 失败: {e}')

print()

# 方式 2: 从子模块导入
print('2. 尝试从 data 导入')
try:
    from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
    print('   ✅ 成功导入 PolymarketDataClient')
except Exception as e:
    print(f'   ❌ 失败: {e}')

print()

# 方式 3: 从 factories 导入
print('3. 尝试从 factories 导入')
try:
    from nautilus_trader.adapters.polymarket.factories import PolymarketLiveDataClientFactory
    print('   ✅ 成功导入 PolymarketLiveDataClientFactory')
except Exception as e:
    print(f'   ❌ 失败: {e}')

print()

# 方式 4: 查看 config
print('4. 尝试从 config 导入')
try:
    from nautilus_trader.adapters.polymarket.config import PolymarketDataClientConfig
    print('   ✅ 成功导入 PolymarketDataClientConfig')
except Exception as e:
    print(f'   ❌ 失败: {e}')

print()

# 方式 5: 使用 py_clob_client
print('5. 直接使用 py_clob_client')
try:
    from py_clob_client import ClobClient
    print('   ✅ 成功导入 ClobClient')
    print(f'   ClobClient 方法: {[x for x in dir(ClobClient) if not x.startswith("_")][:10]}')
except Exception as e:
    print(f'   ❌ 失败: {e}')
