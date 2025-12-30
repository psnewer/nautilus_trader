"""
OrbitExch 配置使用示例

展示如何加载配置
"""

from nautilus_trader.adapters.orbitexch.config_loader import (
    create_data_client_config,
    create_exec_client_config,
    load_config,
)


def example_load_config():
    """示例：加载配置字典"""
    
    # 加载开发环境配置
    config = load_config(env='dev')
    print('开发环境配置:')
    print(f'  Base URL: {config["base_url"]}')
    print(f'  Headless: {config["headless"]}')
    print(f'  Username: {config.get("username", "未设置")}')
    print()


def example_create_client_config():
    """示例：创建客户端配置"""
    
    # 从配置文件 + 环境变量创建
    data_config = create_data_client_config(env='dev')
    
    print('DataClient 配置:')
    print(f'  Username: {data_config.username}')
    print(f'  Base URL: {data_config.base_url}')
    print(f'  Headless: {data_config.headless}')
    print(f'  Scrape interval: {data_config.scrape_interval_ms}ms')
    print()
    
    exec_config = create_exec_client_config(env='dev')
    
    print('ExecClient 配置:')
    print(f'  Max bet: {exec_config.max_bet_amount}')
    print(f'  Confirm bet: {exec_config.confirm_bet}')


if __name__ == '__main__':
    print('=' * 60)
    print('OrbitExch 配置示例')
    print('=' * 60)
    print()
    
    example_load_config()
    example_create_client_config()
    
    print('=' * 60)
    print('提示: 复制 .env.example 为 .env 并填入真实账户信息')
    print('=' * 60)
