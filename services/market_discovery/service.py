# -------------------------------------------------------------------------------------------------
#  Market Discovery Service
#  
#  独立服务进程 - 负责从多个平台发现市场
# -------------------------------------------------------------------------------------------------

"""
市场发现服务

这是一个独立运行的服务，负责:
1. 从多个交易平台持续发现市场
2. 标准化市场数据
3. 通过消息队列/数据库发布市场数据
"""

import asyncio
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from datetime import datetime
import json
from pathlib import Path


@dataclass
class StandardMarket:
    """
    标准化的市场数据结构
    
    适用于所有平台
    """
    # 唯一标识
    platform: str  # 'ORBITEXCH', 'POLYMARKET', etc.
    market_id: str
    event_id: str
    
    # 基本信息
    event_name: str
    market_type: str  # 'MATCH_ODDS', 'OVER_UNDER', etc.
    sport: str  # 'FOOTBALL', 'BASKETBALL', etc.
    
    # 时间
    start_time: str  # ISO format
    discovered_at: str  # ISO format
    
    # 状态
    status: str  # 'OPEN', 'SUSPENDED', 'CLOSED'
    in_play: bool
    
    # 选项 (outcomes)
    outcomes: List[Dict]  # [{'id': str, 'name': str, 'odds': float}, ...]
    
    # 元数据
    metadata: Dict  # 平台特定的额外信息
    
    def to_dict(self):
        return asdict(self)
    
    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class PlatformAdapter:
    """平台适配器基类"""
    
    def __init__(self, platform_name: str):
        self.platform_name = platform_name
        self._log = logging.getLogger(f'Adapter.{platform_name}')
    
    async def discover_markets(self) -> List[StandardMarket]:
        """发现市场 - 子类必须实现"""
        raise NotImplementedError
    
    async def start(self):
        """启动适配器"""
        pass
    
    async def stop(self):
        """停止适配器"""
        pass


class MarketDiscoveryService:
    """
    市场发现服务主类
    
    管理多个平台适配器
    """
    
    def __init__(self, output_dir: str = './data/markets'):
        self.adapters: Dict[str, PlatformAdapter] = {}
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._running = False
        self._log = logging.getLogger('MarketDiscoveryService')
    
    def register_adapter(self, adapter: PlatformAdapter):
        """注册平台适配器"""
        self.adapters[adapter.platform_name] = adapter
        self._log.info(f'✅ 注册适配器: {adapter.platform_name}')
    
    async def start(self):
        """启动服务"""
        self._log.info('🚀 启动市场发现服务...')
        self._running = True
        
        # 启动所有适配器
        for adapter in self.adapters.values():
            await adapter.start()
        
        # 开始发现循环
        asyncio.create_task(self._discovery_loop())
        
        self._log.info('✅ 市场发现服务已启动')
    
    async def stop(self):
        """停止服务"""
        self._log.info('🛑 停止市场发现服务...')
        self._running = False
        
        # 停止所有适配器
        for adapter in self.adapters.values():
            await adapter.stop()
        
        self._log.info('✅ 市场发现服务已停止')
    
    async def _discovery_loop(self):
        """市场发现循环"""
        while self._running:
            try:
                # 从所有平台发现市场
                all_markets = []
                
                for platform_name, adapter in self.adapters.items():
                    try:
                        markets = await adapter.discover_markets()
                        all_markets.extend(markets)
                        self._log.info(f'📊 {platform_name}: 发现 {len(markets)} 个市场')
                    except Exception as e:
                        self._log.error(f'❌ {platform_name} 发现失败: {e}')
                
                # 保存到文件
                if all_markets:
                    self._save_markets(all_markets)
                
                # 等待下一轮
                await asyncio.sleep(60)  # 每分钟更新一次
                
            except Exception as e:
                self._log.error(f'发现循环错误: {e}')
                await asyncio.sleep(10)
    
    def _save_markets(self, markets: List[StandardMarket]):
        """保存市场数据"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # 按平台分组保存
        by_platform = {}
        for market in markets:
            if market.platform not in by_platform:
                by_platform[market.platform] = []
            by_platform[market.platform].append(market.to_dict())
        
        # 保存每个平台的数据
        for platform, platform_markets in by_platform.items():
            filename = self.output_dir / f'{platform.lower()}_{timestamp}.json'
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(platform_markets, f, ensure_ascii=False, indent=2)
            
            self._log.info(f'💾 保存 {platform}: {len(platform_markets)} 个市场  {filename}')
        
        # 保存最新的完整快照
        latest_file = self.output_dir / 'latest_markets.json'
        with open(latest_file, 'w', encoding='utf-8') as f:
            all_data = {
                'timestamp': datetime.now().isoformat(),
                'platforms': list(by_platform.keys()),
                'total_markets': len(markets),
                'markets_by_platform': by_platform,
            }
            json.dump(all_data, f, ensure_ascii=False, indent=2)


# 示例使用
if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print('市场发现服务框架已创建')
    print('请运行具体的适配器实现')
