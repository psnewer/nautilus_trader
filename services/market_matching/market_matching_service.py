"""Market Matching Service - 市场匹配服务（带预处理）"""

import json
import sys
from pathlib import Path
from typing import List, Dict
from dataclasses import dataclass, asdict

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from services.market_matching.matchers import DefaultMatcher
from services.market_matching.preprocessor import EventPreprocessor


@dataclass
class MarketEvent:
    platform: str
    event_id: str
    sport: str
    competition: str
    event: str
    home_team: str
    away_team: str
    metadata: dict


@dataclass
class MatchedPair:
    polymarket_event: MarketEvent
    orbitexch_event: MarketEvent
    match_scores: Dict
    confidence: float


class MarketMatchingService:
    def __init__(self, config=None):
        from services.market_matching.config import MatchConfig
        self.config = config or MatchConfig()
        self.matcher = self._load_matcher(self.config.matcher_class)
        self.polymarket_events = []
        self.orbitexch_events = []
        
        # 初始化预处理器
        if self.config.enable_preprocess:
            self.preprocessor = EventPreprocessor(self.config.preprocess_config)
        else:
            self.preprocessor = None
    
    def _load_matcher(self, matcher_class: str):
        if matcher_class == "DefaultMatcher":
            return DefaultMatcher()
        else:
            raise ValueError(f"未知匹配器: {matcher_class}")
    
    def _load_event(self, event_data: dict) -> MarketEvent:
        platform = event_data.get('platform', 'Unknown')
        event_id = str(event_data.get('event_id', ''))
        sport = event_data.get('sport', '')
        competition = event_data.get('competition', '')
        event = event_data.get('event', '')
        home_team = event_data.get('home_team', '')
        away_team = event_data.get('away_team', '')
        metadata = {}
        standard_fields = {'platform', 'event_id', 'sport', 'competition', 'event', 'home_team', 'away_team', 'metadata'}
        for key, value in event_data.items():
            if key not in standard_fields:
                metadata[key] = value
        if 'metadata' in event_data and isinstance(event_data['metadata'], dict):
            metadata.update(event_data['metadata'])
        return MarketEvent(platform=platform, event_id=event_id, sport=sport, competition=competition, event=event, home_team=home_team, away_team=away_team, metadata=metadata)
    
    def load_events(self, polymarket_file=None, orbitexch_file=None):
        if polymarket_file is None:
            polymarket_file = project_root / "output" / "polymarket_events.json"
        if orbitexch_file is None:
            orbitexch_file = project_root / "output" / "orbitexch_events.json"
        
        poly_path = Path(polymarket_file)
        if poly_path.exists():
            with open(poly_path, 'r', encoding='utf-8') as f:
                poly_data = json.load(f)
                self.polymarket_events = [self._load_event(e) for e in poly_data]
            print(f"✓ 加载 {len(self.polymarket_events)} 个 Polymarket 事件")
        else:
            print(f"✗ 未找到: {poly_path}")
        
        orbit_path = Path(orbitexch_file)
        if orbit_path.exists():
            with open(orbit_path, 'r', encoding='utf-8') as f:
                orbit_data = json.load(f)
                self.orbitexch_events = [self._load_event(e) for e in orbit_data]
            print(f"✓ 加载 {len(self.orbitexch_events)} 个 OrbitExch 事件")
        else:
            print(f"✗ 未找到: {orbit_path}")
        
        # 预处理事件
        if self.preprocessor:
            print(f"\n{'='*60}")
            print("预处理阶段")
            print(f"{'='*60}")
            self.preprocessor.preprocess_events(self.polymarket_events, 'Polymarket')
            self.preprocessor.preprocess_events(self.orbitexch_events, 'OrbitExch')
    
    def _calculate_confidence(self, scores: Dict) -> float:
        if not scores:
            return 0.0
        total_match = total_char = field_count = 0
        all_matched = True
        for field, value in scores.items():
            if field == 'team_swapped' or not isinstance(value, tuple):
                continue
            match_count, char_count = value
            if match_count == 0:
                all_matched = False
            total_match += match_count
            total_char += char_count
            field_count += 1
        if field_count == 0 or not all_matched:
            return 0.0
        base_confidence = 0.6
        avg_char = total_char / field_count
        char_bonus = min(0.4, (avg_char - 2) / 20)
        return min(1.0, base_confidence + char_bonus)
    
    def match_events(self) -> List[MatchedPair]:
        matches = []
        print(f"\n{'='*60}")
        print("匹配阶段")
        print(f"{'='*60}")
        print(f"开始匹配 {len(self.polymarket_events)} x {len(self.orbitexch_events)} 个事件...")
        print(f"匹配器: {self.config.matcher_class}")
        print(f"最低置信度: {self.config.min_confidence}\n")
        
        match_count = 0
        for poly_event in self.polymarket_events:
            for orbit_event in self.orbitexch_events:
                poly_dict = asdict(poly_event)
                orbit_dict = asdict(orbit_event)
                config_dict = asdict(self.config)
                is_match, scores = self.matcher.match(poly_dict, orbit_dict, config_dict)
                if not is_match:
                    continue
                confidence = self._calculate_confidence(scores)
                if confidence < self.config.min_confidence:
                    continue
                matched_pair = MatchedPair(polymarket_event=poly_event, orbitexch_event=orbit_event, match_scores=scores, confidence=confidence)
                matches.append(matched_pair)
                match_count += 1
                if self.config.verbose:
                    swapped = " 🔄" if scores.get('team_swapped') else ""
                    print(f"[{match_count}] {poly_event.event}")
                    print(f"     <-> {orbit_event.event}{swapped}")
                    print(f"     置信度: {confidence:.2f}")
        
        print(f"\n✓ 找到 {len(matches)} 个匹配")
        return matches
    
    def save_matches(self, matches: List[MatchedPair], output_file=None):
        if output_file is None:
            output_file = project_root / "output" / "market_matches.json"
        output_data = []
        for m in matches:
            output_data.append({'polymarket_event': asdict(m.polymarket_event), 'orbitexch_event': asdict(m.orbitexch_event), 'match_scores': {k: list(v) if isinstance(v, tuple) else v for k, v in m.match_scores.items()}, 'confidence': m.confidence})
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
        print(f"\n✓ 结果已保存: {output_path}")


def main():
    from services.market_matching.config import MatchConfig
    print("=" * 60)
    print("Market Matching Service (带预处理)")
    print("=" * 60)
    
    config = MatchConfig(
        match_sport=True,
        match_competition=True,
        match_home_team=True,
        match_away_team=True,
        allow_team_swap=True,
        min_confidence=0.6,
        verbose=True,
        matcher_class="DefaultMatcher",
        enable_preprocess=True  # 启用预处理
    )
    
    service = MarketMatchingService(config)
    service.load_events()
    matches = service.match_events()
    service.save_matches(matches)
    
    print("\n" + "=" * 60)
    print("匹配完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
