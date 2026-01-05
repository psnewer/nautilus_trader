# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------------------------
#  Polymarket 适配器 - 使用 requests
# -------------------------------------------------------------------------------------------------

"""
Polymarket 市场发现适配器

使用 Python requests 库，更稳定
"""

import requests
import logging
from typing import List, Optional
from dataclasses import dataclass, asdict, field
from datetime import datetime
import re


@dataclass
class StandardEvent:
    """标准化的事件数据"""
    platform: str
    event_id: str
    event_name: str
    sport: str
    competition: str
    home_team: Optional[str] = None
    away_team: Optional[str] = None
    participants: List[str] = field(default_factory=list)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    is_live: bool = False
    is_active: bool = True
    discovered_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict = field(default_factory=dict)
    
    def to_dict(self):
        return asdict(self)


def extract_teams_from_name(event_name: str):
    """从事件名称提取球队"""
    separators = [
        r'\s+vs\.?\s+',
        r'\s+v\.?\s+',
        r'\s+@\s+',
        r'\s+-\s+',
        r'\s+at\s+',
    ]
    for sep_pattern in separators:
        match = re.split(sep_pattern, event_name, flags=re.IGNORECASE)
        if len(match) == 2:
            return match[0].strip(), match[1].strip()
    return None, None


@dataclass
class PolymarketRawMarket:
    """Polymarket 原始市场数据"""
    market_id: str
    question: str
    description: str
    end_date: Optional[str]
    active: bool
    closed: bool
    tags: List[str]
    outcomes: List[str]
    volume: float
    liquidity: Optional[float]
    
    def to_dict(self):
        return asdict(self)


class PolymarketAdapter:
    """Polymarket 适配器"""
    
    def __init__(self, api_type: str = 'clob'):
        """
        Parameters
        ----------
        api_type : str
            'gamma' or 'clob'
        """
        self.api_type = api_type
        self._log = logging.getLogger('PolymarketAdapter')
        
        if api_type == 'gamma':
            self.base_url = 'https://gamma-api.polymarket.com'
        elif api_type == 'clob':
            self.base_url = 'https://clob.polymarket.com'
        else:
            raise ValueError(f"api_type must be 'gamma' or 'clob', got: {api_type}")
        
        # Create session
        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'application/json',
        })
    
    def discover_markets(
        self,
        limit: int = 100,
        sports_only: bool = True,
        active_only: bool = True
    ) -> List[StandardEvent]:
        """
        Discover markets
        
        Parameters
        ----------
        limit : int
            Maximum number of markets to return
        sports_only : bool
            Only return sports markets
        active_only : bool
            Only return active (not closed) markets
            
        Returns
        -------
        List[StandardEvent]
            List of standardized events
        """
        self._log.info(f'Discovering Polymarket markets (api={self.api_type}, limit={limit})...')
        
        # Fetch raw data
        raw_markets = self._fetch_raw_markets(limit, active_only)
        
        if not raw_markets:
            self._log.warning('No markets found')
            return []
        
        self._log.info(f'Found {len(raw_markets)} raw markets')
        
        # Filter sports markets
        if sports_only:
            raw_markets = self._filter_sports_markets(raw_markets)
            self._log.info(f'After filtering: {len(raw_markets)} sports markets')
        
        # Convert to standard format
        events = []
        for raw_market in raw_markets:
            event = self._convert_to_standard_event(raw_market)
            if event:
                events.append(event)
        
        self._log.info(f'Converted to {len(events)} standard events')
        return events
    
    def _fetch_raw_markets(self, limit: int, active_only: bool) -> List[PolymarketRawMarket]:
        """Fetch raw market data"""
        
        # Build params
        params = {'limit': limit}
        if active_only:
            params['closed'] = 'false'
        
        url = f'{self.base_url}/markets'
        
        try:
            self._log.info(f'Request: {url}')
            self._log.info(f'Params: {params}')
            
            response = self.session.get(url, params=params, timeout=30)
            
            self._log.info(f'Status code: {response.status_code}')
            
            if response.status_code != 200:
                self._log.error(f'Request failed: {response.status_code}')
                self._log.error(f'Response: {response.text[:500]}')
                return []
            
            data = response.json()
            
            # Parse based on API type
            if self.api_type == 'clob':
                # CLOB API: {data: [...], next_cursor: ...}
                market_list = data.get('data', [])
                self._log.info(f'CLOB returned: {len(market_list)} markets')
            else:
                # Gamma API: [...]
                if isinstance(data, list):
                    market_list = data
                elif isinstance(data, dict) and 'data' in data:
                    market_list = data['data']
                else:
                    self._log.error(f'Unknown response format: {type(data)}')
                    return []
                self._log.info(f'Gamma returned: {len(market_list)} markets')
            
            # Parse each market
            raw_markets = []
            for item in market_list:
                try:
                    raw_market = self._parse_market_item(item)
                    if raw_market:
                        raw_markets.append(raw_market)
                except Exception as e:
                    self._log.debug(f'Failed to parse market: {e}')
                    continue
            
            return raw_markets
        
        except requests.Timeout:
            self._log.error('Request timeout')
            return []
        except requests.RequestException as e:
            self._log.error(f'Request failed: {e}')
            return []
        except Exception as e:
            self._log.error(f'Unknown error: {e}')
            import traceback
            traceback.print_exc()
            return []
    
    def _parse_market_item(self, item: dict) -> Optional[PolymarketRawMarket]:
        """Parse a single market item"""
        
        # CLOB and Gamma API have slightly different fields
        if self.api_type == 'clob':
            market_id = item.get('condition_id', '')
            question = item.get('question', '')
            description = item.get('description', '')
            end_date = item.get('end_date_iso')
            active = item.get('active', False)
            closed = item.get('closed', False)
            tags = item.get('tags', [])
            outcomes = item.get('outcomes', [])
            volume = float(item.get('volume', 0))
            liquidity = None
        
        else:  # gamma
            market_id = item.get('id', item.get('conditionId', ''))
            question = item.get('question', '')
            description = item.get('description', '')
            end_date = item.get('endDate', item.get('end_date_iso'))
            active = item.get('active', not item.get('closed', False))
            closed = item.get('closed', False)
            tags = item.get('tags', [])
            
            # Gamma API outcomes
            if 'outcomes' in item:
                outcomes = item['outcomes']
            elif 'outcomePrices' in item:
                outcomes = list(item.get('outcomePrices', {}).keys())
            elif 'tokens' in item:
                outcomes = [t.get('outcome', '') for t in item.get('tokens', [])]
            else:
                outcomes = []
            
            volume = float(item.get('volume', item.get('volume24hr', 0)))
            liquidity = float(item.get('liquidity', 0)) if 'liquidity' in item else None
        
        if not question:
            return None
        
        return PolymarketRawMarket(
            market_id=str(market_id),
            question=question,
            description=description,
            end_date=end_date,
            active=active,
            closed=closed,
            tags=tags if isinstance(tags, list) else [],
            outcomes=outcomes if isinstance(outcomes, list) else [],
            volume=volume,
            liquidity=liquidity,
        )
    
    def _filter_sports_markets(self, markets: List[PolymarketRawMarket]) -> List[PolymarketRawMarket]:
        """Filter sports markets"""
        
        sports_keywords = [
            # Sports types
            'nfl', 'nba', 'mlb', 'nhl', 'soccer', 'football', 'basketball', 
            'baseball', 'hockey', 'tennis', 'golf', 'boxing', 'mma', 'ufc',
            'ncaab', 'ncaaf',
            # Leagues
            'premier league', 'champions league', 'la liga', 'bundesliga',
            'super bowl', 'world cup', 'playoffs',
            # Verbs
            'win', 'beat', 'defeat', 'vs', 'v ', ' v.', '@',
            # Other
            'game', 'match', 'championship', 'tournament',
        ]
        
        # Exclude keywords
        exclude_keywords = [
            'election', 'presidential', 'senate', 'congress',
            'stock', 'bitcoin', 'crypto', 'price',
            'award', 'oscar', 'grammy',
        ]
        
        sports_markets = []
        
        for market in markets:
            question_lower = market.question.lower()
            tags_lower = ' '.join(market.tags).lower()
            desc_lower = market.description.lower()
            
            # Check exclude keywords
            is_excluded = any(
                keyword in question_lower or keyword in desc_lower
                for keyword in exclude_keywords
            )
            
            if is_excluded:
                continue
            
            # Check if contains sports keywords
            is_sports = any(
                keyword in question_lower or 
                keyword in tags_lower or 
                keyword in desc_lower
                for keyword in sports_keywords
            )
            
            if is_sports:
                sports_markets.append(market)
        
        return sports_markets
    
    def _convert_to_standard_event(self, raw_market: PolymarketRawMarket) -> Optional[StandardEvent]:
        """Convert to standard event format"""
        
        # Extract teams from question
        home_team, away_team = extract_teams_from_name(raw_market.question)
        
        # Detect sport type
        sport = self._detect_sport(raw_market.question, raw_market.tags)
        
        # Detect competition
        competition = self._detect_competition(raw_market.question, raw_market.description)
        
        event = StandardEvent(
            platform='Polymarket',
            event_id=raw_market.market_id,
            event_name=raw_market.question,
            sport=sport,
            competition=competition,
            home_team=home_team,
            away_team=away_team,
            participants=[],
            start_time=raw_market.end_date,
            end_time=raw_market.end_date,
            is_live=False,
            is_active=raw_market.active and not raw_market.closed,
            metadata={
                'question': raw_market.question,
                'description': raw_market.description,
                'tags': raw_market.tags,
                'outcomes': raw_market.outcomes,
                'volume': raw_market.volume,
                'liquidity': raw_market.liquidity,
            }
        )
        
        return event
    
    def _detect_sport(self, question: str, tags: List[str]) -> str:
        """Detect sport type"""
        
        question_lower = question.lower()
        tags_lower = ' '.join(tags).lower()
        combined = question_lower + ' ' + tags_lower
        
        sport_patterns = {
            'Soccer': ['soccer', 'football', 'premier league', 'champions league', 'la liga', 'bundesliga', 'serie a'],
            'Basketball': ['nba', 'basketball', 'ncaab'],
            'American Football': ['nfl', 'ncaaf', 'super bowl'],
            'Baseball': ['mlb', 'baseball'],
            'Ice Hockey': ['nhl', 'hockey'],
            'Tennis': ['tennis', 'atp', 'wta'],
            'Golf': ['golf', 'pga'],
            'Boxing': ['boxing'],
            'MMA': ['mma', 'ufc'],
        }
        
        for sport, keywords in sport_patterns.items():
            if any(keyword in combined for keyword in keywords):
                return sport
        
        return 'Other'
    
    def _detect_competition(self, question: str, description: str) -> str:
        """Detect competition name"""
        
        combined = (question + ' ' + description).lower()
        
        competitions = {
            'English Premier League': ['premier league', 'epl'],
            'UEFA Champions League': ['champions league', 'ucl'],
            'La Liga': ['la liga'],
            'Bundesliga': ['bundesliga'],
            'NBA': ['nba'],
            'NFL': ['nfl'],
            'MLB': ['mlb'],
            'NHL': ['nhl'],
            'NCAA Basketball': ['ncaab'],
            'NCAA Football': ['ncaaf'],
        }
        
        for comp_name, keywords in competitions.items():
            if any(keyword in combined for keyword in keywords):
                return comp_name
        
        return ''


def test_adapter():
    """Test adapter"""
    import logging
    import json
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print('=' * 70)
    print('Test Polymarket Adapter (using requests)')
    print('=' * 70)
    print()
    
    # Test Gamma API
    print('1. Test Gamma API')
    print('-' * 70)
    adapter_gamma = PolymarketAdapter(api_type='gamma')
    events_gamma = adapter_gamma.discover_markets(limit=100, sports_only=True, active_only=True)
    
    print(f'\nGamma API: {len(events_gamma)} active sports events')
    
    if events_gamma:
        print('\nSample events:')
        for i, event in enumerate(events_gamma[:5], 1):
            print(f'{i}. {event.event_name[:80]}')
            print(f'   Sport: {event.sport} | Competition: {event.competition}')
            if event.home_team:
                print(f'   Teams: {event.home_team} vs {event.away_team}')
        
        with open('polymarket_gamma_events.json', 'w', encoding='utf-8') as f:
            json.dump([e.to_dict() for e in events_gamma], f, indent=2, ensure_ascii=False)
        print('\nSaved to polymarket_gamma_events.json')
    
    # Test CLOB API
    print('\n' + '=' * 70)
    print('2. Test CLOB API')
    print('-' * 70)
    adapter_clob = PolymarketAdapter(api_type='clob')
    events_clob = adapter_clob.discover_markets(limit=100, sports_only=True, active_only=True)
    
    print(f'\nCLOB API: {len(events_clob)} active sports events')
    
    if events_clob:
        print('\nSample events:')
        for i, event in enumerate(events_clob[:5], 1):
            print(f'{i}. {event.event_name[:80]}')
            print(f'   Sport: {event.sport} | Competition: {event.competition}')
            if event.home_team:
                print(f'   Teams: {event.home_team} vs {event.away_team}')
        
        with open('polymarket_clob_events.json', 'w', encoding='utf-8') as f:
            json.dump([e.to_dict() for e in events_clob], f, indent=2, ensure_ascii=False)
        print('\nSaved to polymarket_clob_events.json')


if __name__ == '__main__':
    test_adapter()
