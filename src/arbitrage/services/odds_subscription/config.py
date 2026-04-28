"""
赔率订阅服务配置

实际定义已移至 src/arbitrage/common/subscription_config.py，
这里保留位置作为服务侧入口（向后兼容）。
"""

from src.arbitrage.common.subscription_config import OddsSubscriptionConfig

__all__ = ["OddsSubscriptionConfig"]
