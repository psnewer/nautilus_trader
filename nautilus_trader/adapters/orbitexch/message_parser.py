# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
# -------------------------------------------------------------------------------------------------

"""OrbitExch WebSocket 消息解析器"""

from typing import Dict, List, Any, Optional
import logging


class OrbitExchMessageParser:
    """
    解析 OrbitExch WebSocket 消息
    
    消息结构示例:
    {
        'id': '1.252123015',                    # Market ID
        'mainEventId': '35093863',              # Event ID
        'mainEventName': 'Team A v Team B',     # Event Name
        'marketNameWithParents': 'Match Odds',  # Market Type
        'rc': [                                 # Runners (selections)
            {
                'id': 61640820,                 # Selection ID
                'bdatb': [                      # Back prices (best available to back)
                    {'index': 0, 'odds': 2.26, 'amount': 80.59},
                    ...
                ],
                'bdatl': [                      # Lay prices (best available to lay)
                    {'index': 0, 'odds': 2.36, 'amount': 66.46},
                    ...
                ],
                'tv': 469.93,                   # Total volume
                'locked': False
            },
            ...
        ],
        'marketDefinition': {
            'marketType': 'MATCH_ODDS',
            'status': 'OPEN',
            'inPlay': False,
            'runners': [
                {'selectionId': 61640820, 'status': 'ACTIVE'},
                ...
            ]
        }
    }
    """
    
    def __init__(self):
        self._log = logging.getLogger(self.__class__.__name__)
    
    def parse_price_message(self, message: Dict) -> Optional[Dict[str, Any]]:
        """
        解析赔率消息
        
        Parameters
        ----------
        message : dict
            原始 WebSocket 消息
            
        Returns
        -------
        Dict or None
            解析后的赔率数据
        """
        try:
            # 基本信息
            market_id = message.get('id')
            if not market_id:
                return None
            
            event_id = message.get('mainEventId')
            event_name = message.get('mainEventName', 'Unknown')
            market_name = message.get('marketNameWithParents', 'Unknown')
            
            # 市场状态
            market_def = message.get('marketDefinition', {})
            status = market_def.get('status', 'UNKNOWN')
            in_play = market_def.get('inPlay', False)
            
            # 解析选手/赔率
            runners = []
            rc = message.get('rc', [])
            
            for runner in rc:
                selection_id = str(runner.get('id'))
                
                # Back odds (可以买入的赔率)
                back_prices = []
                for item in runner.get('bdatb', []):
                    back_prices.append({
                        'price': float(item.get('odds', 0)),
                        'size': float(item.get('amount', 0)),
                    })
                
                # Lay odds (可以卖出的赔率)
                lay_prices = []
                for item in runner.get('bdatl', []):
                    lay_prices.append({
                        'price': float(item.get('odds', 0)),
                        'size': float(item.get('amount', 0)),
                    })
                
                # 总成交量
                total_volume = float(runner.get('tv', 0))
                
                runners.append({
                    'selection_id': selection_id,
                    'back': back_prices,
                    'lay': lay_prices,
                    'total_volume': total_volume,
                    'locked': runner.get('locked', False),
                })
            
            return {
                'market_id': market_id,
                'event_id': event_id,
                'event_name': event_name,
                'market_name': market_name,
                'status': status,
                'in_play': in_play,
                'runners': runners,
                'timestamp': message.get('apiPt'),  # API timestamp
            }
        
        except Exception as e:
            self._log.error(f'解析赔率消息失败: {e}')
            return None
    
    def parse_general_frame(self, message: Dict) -> Optional[Dict[str, Any]]:
        """
        解析 `general` 频道下行帧(SockJS `a[...]` 已由 websocket_handler 解包)。

        实测帧(2026-05-22 用户登录刷新页面抓取)按**顶层 key** 分型:
        - `{"BALANCE": {"balance": "37.49", "avBalance": null}}` → 账户余额
          (`balance` 是**字符串**;该值 WS 侧已含挂单占用,RiskEngine 不再减,Q17)
        - `{"CURRENT_BETS": [<bet>, ...]}` → 当前注单(空时 `[]`)
        `general` 频道**时不时还有其它类型**的帧 → 未知 key 一律返回 None 忽略。

        bet item 字段(**工作假设,待 populated 抓帧确认**):与 REST `/customer/api/currentBets`
        的 `bets[]` 同源(见 executor.get_current_bets),即 `marketId`/`selectionId`/
        `sizeMatched`/`averagePrice`/`side`/`profitNet`/`liability`(参旧 load_orbitexch_bets)。

        Returns
        -------
        Dict or None
            `{"type": "balance", "balance": float|None, "av_balance": ...}` 或
            `{"type": "current_bets", "bets": list}`;未知帧 → None。
        """
        if not isinstance(message, dict):
            return None

        if 'BALANCE' in message:
            payload = message.get('BALANCE') or {}
            return {
                'type': 'balance',
                'balance': self._to_float(payload.get('balance')),
                'av_balance': self._to_float(payload.get('avBalance')),
            }

        if 'CURRENT_BETS' in message:
            return {
                'type': 'current_bets',
                'bets': message.get('CURRENT_BETS') or [],
            }

        self._log.debug(f'未知 general 帧,忽略: {str(message)[:120]}')
        return None

    # 兼容旧名(原 TODO stub);新代码用 parse_general_frame
    def parse_order_message(self, message: Dict) -> Optional[Dict[str, Any]]:
        return self.parse_general_frame(message)

    @staticmethod
    def _to_float(value) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    
    def get_best_back_price(self, runner: Dict) -> Optional[float]:
        """获取最佳 back 价格 (index=0)"""
        back = runner.get('back', [])
        if back and len(back) > 0:
            return back[0].get('price')
        return None
    
    def get_best_lay_price(self, runner: Dict) -> Optional[float]:
        """获取最佳 lay 价格 (index=0)"""
        lay = runner.get('lay', [])
        if lay and len(lay) > 0:
            return lay[0].get('price')
        return None
    
    def get_runner_by_selection_id(
        self,
        parsed_message: Dict,
        selection_id: str
    ) -> Optional[Dict]:
        """根据 selection_id 获取 runner 数据"""
        for runner in parsed_message.get('runners', []):
            if runner.get('selection_id') == selection_id:
                return runner
        return None
