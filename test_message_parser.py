"""测试消息解析器"""

import json
from nautilus_trader.adapters.orbitexch.message_parser import OrbitExchMessageParser


def test_parser():
    """测试解析器"""
    
    # 加载样本数据
    with open('docs/ws_prices.json', 'r', encoding='utf-8') as f:
        messages = json.load(f)
    
    parser = OrbitExchMessageParser()
    
    print('=' * 70)
    print('OrbitExch 消息解析器测试')
    print('=' * 70)
    print()
    
    # 解析第一条消息
    if messages:
        first_msg = messages[0]
        parsed = parser.parse_price_message(first_msg)
        
        if parsed:
            print(f'Market ID: {parsed["market_id"]}')
            print(f'Event: {parsed["event_name"]}')
            print(f'Market: {parsed["market_name"]}')
            print(f'Status: {parsed["status"]}')
            print(f'In-Play: {parsed["in_play"]}')
            print()
            
            print(f'Runners: {len(parsed["runners"])}')
            for i, runner in enumerate(parsed['runners'], 1):
                print(f'  {i}. Selection {runner["selection_id"]}:')
                
                # Best back
                best_back = parser.get_best_back_price(runner)
                if best_back:
                    back_size = runner['back'][0]['size']
                    print(f'     Back: {best_back} @ {back_size}')
                
                # Best lay
                best_lay = parser.get_best_lay_price(runner)
                if best_lay:
                    lay_size = runner['lay'][0]['size']
                    print(f'     Lay:  {best_lay} @ {lay_size}')
                
                print(f'     Volume: {runner["total_volume"]}')
        
        print()
        print('=' * 70)
        print(f'测试完成！成功解析 {len(messages)} 条消息')
        print('=' * 70)


if __name__ == '__main__':
    test_parser()
