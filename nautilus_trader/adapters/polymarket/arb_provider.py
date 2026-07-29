# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------

"""
ArbPolymarketInstrumentProvider —— PM 端市场发现 + matching info 补全。

设计见 `docs/arbitrage/architectures/discovery/architecture.md §3.2`。

**#57/#289(series_id 全量查询;撤掉 #55 的 /series/{id} 截断跳)**:
#55 链路漏主赛事 —— 根因是 `/series/{id}?closed=false` **只内嵌截断的 ~10 条 events**(默认页
又被 `limit=20` 截,与页面"懒加载"同源)。改 `load_all_async` 整体 override:

发现链路:
  /sports                          → 每 competition:`sport`(如 "atp")+ series id + ordering
    └─ 按 ArbContext.target_competitions_by_data_source["PMSPORTS"] 过滤
    └─ ordering(home/away)是 **competition 特异**属性(ATP=home / MLB=away),决定 2-way outcomes 排列
  /events/keyset?series_id={id}&closed=false&active=true
    └─ 游标分页拉全本 series 的 H2H 比赛(含主赛事;每 event **内嵌** teams + markets,无需二次 /events?id=)
    └─ 每 event 筛 markets 内 sportsMarketType == "moneyline"

  注:series_slug 不通用(只 atp/wta 的 series slug 恰好 == league slug;足球/棒球查 == 0),故仍走
      /sports 取 series id;`/events/keyset?series_id=` 内嵌 teams,所以不再二跳 /events?id=。

队名(权威源 event["teams"]:name + ordering + abbreviation;缺则 fallback `_parse_team_names(title)`)。
  **不用** outcomes 顺序当队名(3-way 是 Yes/No;2-way 顺序随 competition ordering 翻)。

selection_role:
  2-way / 单市场 3-outcome(slug == ticker):按 competition ordering 选映射 ——
    ordering=home → outcomes=[home,(draw),away];ordering=away → 反排([away,(draw),home])。
  3-way binary(slug == ticker-{abbr}):仅 "Yes" token;{abbr} 取自 teams.abbreviation。
    ticker-{home_abbr}→home / ticker-{away_abbr}→away / ticker-draw→draw;其它(No token)→ 跳过。

home_abbr/away_abbr 优先 teams.abbreviation,缺则 event ticker 拆 `-` 的 parts[1]/parts[2]。

matching info:home/away←teams;selection_role←slug+ordering;competition←config 原名(matching aliases 标准化);
       sport←config competition→sport map。
"""

from __future__ import annotations

import re

from nautilus_trader.adapters.polymarket.common.gamma_markets import fetch_gamma_events_keyset
from nautilus_trader.adapters.polymarket.common.gamma_markets import fetch_gamma_json
from nautilus_trader.adapters.polymarket.common.gamma_markets import (
    normalize_gamma_market_to_clob_format,
)
from nautilus_trader.adapters.polymarket.common.parsing import parse_polymarket_instrument
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProvider

_GAMMA_BASE = "https://gamma-api.polymarket.com"

# ─── 队名解析(平移旧 scraper.py:_parse_team_names）────────────────────────


def _parse_team_names(title: str) -> tuple[str, str] | None:
    """从 event title 解析 (home_team, away_team);无法解析返 None。

    平移旧 `scraper.py`:去前缀 / 去 ", scheduled for" / 去 "?";按 vs./" vs "/正则 vs 拆;
    `:` / `-` 清洗(主队留分隔符后,客队留分隔符前)。
    """
    cleaned = title
    for prefix in ("Who will win the ", "Who will win ", "draft "):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    if ", scheduled for" in cleaned:
        cleaned = cleaned.split(", scheduled for")[0]
    cleaned = cleaned.rstrip("?").strip()

    if "vs." in cleaned:
        parts = cleaned.split("vs.", 1)
    elif " vs " in cleaned:
        parts = cleaned.split(" vs ", 1)
    elif "vs" in cleaned.lower():
        m = re.split(r"\bvs\b", cleaned, maxsplit=1, flags=re.IGNORECASE)
        parts = m if len(m) == 2 else None
    else:
        parts = None
    if not parts or len(parts) != 2:
        return None

    home = _clean_team_name(parts[0].strip(), keep_after=True)
    away = _clean_team_name(parts[1].strip(), keep_after=False)
    return (home, away)


def _clean_team_name(name: str, keep_after: bool) -> str:
    """`:` / `-` 清洗:第一个分隔符,keep_after=True 留后段(主队去前缀),False 留前段(客队去后缀)。"""
    colon = name.find(":")
    dash = name.find("-")
    candidates = [p for p in (colon, dash) if p >= 0]
    if not candidates:
        return name.strip()
    sep = min(candidates)
    return (name[sep + 1:] if keep_after else name[:sep]).strip()


# ─── 队名(权威源:event["teams"],#56)+ ticker abbr fallback ───────────────


def _teams_from_event(event: dict) -> tuple[str, str, str, str] | None:
    """从 event["teams"](带 `ordering: home/away` + `name` + `abbreviation`)取权威队名 + abbr。

    返 (home_name, away_name, home_abbr, away_abbr);teams 缺 / 不全 → None(调用方 fallback title)。
    2-way / 3-way 统一用此源(比 title 解析 / outcomes 可靠)。
    """
    teams = event.get("teams") or []
    home = next((t for t in teams if t.get("ordering") == "home"), None)
    away = next((t for t in teams if t.get("ordering") == "away"), None)
    if home and away and home.get("name") and away.get("name"):
        return (
            home["name"],
            away["name"],
            (home.get("abbreviation") or "").lower(),
            (away.get("abbreviation") or "").lower(),
        )
    return None


def _ticker_abbrs(event_ticker: str) -> tuple[str, str]:
    """fallback:event ticker 拆 home/away abbr:`epl-bur-wol-2026-05-24` → (bur, wol)。"""
    parts = event_ticker.split("-")
    home_abbr = parts[1].lower() if len(parts) > 1 else ""
    away_abbr = parts[2].lower() if len(parts) > 2 else ""
    return home_abbr, away_abbr


# ─── selection_role 解析(moneyline slug vs ticker 后缀,平移旧 get_event_tokens）──


def _role_and_claim_for_token(
    *,
    market_slug: str,
    event_ticker: str,
    outcome: str,
    outcomes: list,
    ordering: str,
    home_abbr: str,
    away_abbr: str,
) -> tuple[str, str]:
    """返该 token 的 (selection_role, claim);role 为空表示跳过(非 moneyline 主市场方向)。

    claim:每个 binary pair 都统一为 yes/no。2-way 单市场按 selection_role 定向
    home=yes、away=no；3-way binary market 使用 venue 原生 Yes/No token。
    `ordering`:competition 特异(/sports 字段)—— "home" → outcomes 排 [home,(draw),away];
    "away" → 反排 [away,(draw),home](如 MLB)。`home_abbr`/`away_abbr` 优先 teams.abbreviation。
    """
    market_slug = (market_slug or "").lower()
    event_ticker = (event_ticker or "").lower()
    if not event_ticker:
        return "", ""

    # 2-way / 单市场 3-outcome:slug == ticker。outcomes 排列由 competition ordering 决定
    # (MLB ordering=away → [away, home],按固定下标会错位),故按 ordering 选映射。
    if market_slug == event_ticker:
        try:
            idx = outcomes.index(outcome)
        except ValueError:
            return "", ""
        home_first = (ordering or "home").lower() != "away"
        if len(outcomes) == 2:
            roles = ("home", "away") if home_first else ("away", "home")
            if idx >= 2:
                return "", ""
            role = roles[idx]
            return role, "yes" if role == "home" else "no"
        if len(outcomes) == 3:
            roles = ("home", "draw", "away") if home_first else ("away", "draw", "home")
            return (roles[idx], "") if idx < 3 else ("", "")
        return "", ""

    # 3-way binary:slug == ticker-{abbr};#228 起 YES/NO token 都产腿(claim 区分)
    claim = {"Yes": "yes", "No": "no"}.get(outcome, "")
    if not claim:
        return "", ""
    if home_abbr and market_slug == f"{event_ticker}-{home_abbr}":
        return "home", claim
    if away_abbr and market_slug == f"{event_ticker}-{away_abbr}":
        return "away", claim
    if market_slug == f"{event_ticker}-draw":
        return "draw", claim
    return "", ""


class ArbPolymarketInstrumentProvider(PolymarketInstrumentProvider):
    """PM series-based 发现 + matching info 补全(#55 重写)。`load_all_async` 整体 override。"""

    async def load_all_async(self, filters: dict | None = None) -> None:
        from src.arbitrage.bootstrap import get_arb_context
        from src.arbitrage.common.venues import POLYMARKET
        from src.arbitrage.common.venues import SPORTS_CLIENT

        ctx = get_arb_context()
        target_competitions = (
            (getattr(ctx, "target_competitions_by_data_source", None) or {}).get(
                SPORTS_CLIENT,
                [],
            )
        )
        comp_to_sport = dict(
            (getattr(ctx, "competition_to_sport_by_data_source", None) or {}).get(
                SPORTS_CLIENT,
                {},
            ),
        )
        target_comps = {str(c).lower() for c in target_competitions}
        # #56:competition_aliases 镜像 OE provider(slice 7A)—— 写 info["competition"] 时标准化,
        # 让 matching 的 (sport, competition) 分组两边对得上(PM "atp" / OE "Men's RG 2026" → 都 "ATP")。
        comp_aliases = dict(
            (getattr(ctx, "competition_aliases_by_venue", None) or {}).get(
                POLYMARKET,
                {},
            ),
        )
        if not target_comps:
            self._log.info("PM discovery: no target competitions configured → load 0")
            return

        # Gamma discovery 与 CLOB 主链共用 NT HttpClient(proxy/timeout 由 factory 注入)
        client = self._http_client
        sports = await self._fetch_json(client, f"{_GAMMA_BASE}/sports")
        count = 0
        for comp_info in sports or []:
            comp_raw = str(comp_info.get("sport", ""))   # API `sport` 字段实为 competition 缩写(如 "atp")
            series_id = comp_info.get("series")
            if comp_raw.lower() not in target_comps or not series_id:
                continue
            sport = comp_to_sport.get(comp_raw.lower(), comp_raw)
            # 写 info 用 aliased competition(matching 分组键);raw 只用于 /sports 比对 + sport 查表
            competition = comp_aliases.get(comp_raw, comp_aliases.get(comp_raw.lower(), comp_raw))
            # ordering:competition 特异(ATP=home / MLB=away),决定 2-way outcomes 映射
            ordering = str(comp_info.get("ordering") or "home")
            count += await self._load_series(
                client, str(series_id), competition, sport, ordering,
            )
        self._log.info(f"PM discovery: loaded {count} instruments")

    async def _load_series(
        self, client, series_id: str, comp_name: str, sport: str, ordering: str,
    ) -> int:
        # 旧 /events 单个大响应已 deprecated，经代理回源时还会偶发传输解码失败。
        try:
            events = await fetch_gamma_events_keyset(
                client,
                {
                    "series_id": series_id,
                    "closed": "false",
                    "active": "true",
                },
                base_url=_GAMMA_BASE,
            )
        except Exception as e:
            self._log.error(
                "PM discovery fetch failed "
                f"{_GAMMA_BASE}/events/keyset series_id={series_id}: {e}",
            )
            return 0
        count = 0
        for event in events or []:
            if event.get("closed") or not event.get("active", True):
                continue
            count += self._process_event(event, comp_name, sport, ordering)
        return count

    def _process_event(self, event: dict, comp_name: str, sport: str, ordering: str) -> int:
        title = event.get("title", "")
        event_ticker = (event.get("ticker") or "").lower()

        # #56:队名权威源 event["teams"](带 ordering + name + abbreviation);缺则 fallback title 解析
        teams_info = _teams_from_event(event)
        if teams_info is not None:
            home_team, away_team, home_abbr, away_abbr = teams_info
        else:
            parsed = _parse_team_names(title)
            if parsed is None:
                return 0   # teams 缺 + title 无法解析 → 非 match 事件 / outright winner,跳过
            home_team, away_team = parsed
            home_abbr, away_abbr = _ticker_abbrs(event_ticker)   # fallback abbr 拆 ticker

        game_id = event.get("gameId")   # #60:== sports WS gameId(eviction/strategy 映射键)
        count = 0
        for market in event.get("markets", []):
            # 修正点①:只取 moneyline 主市场(胜平负)
            if market.get("sportsMarketType") != "moneyline":
                continue
            count += self._load_moneyline_market(
                market=market,
                event_ticker=event_ticker,
                comp_name=comp_name,
                sport=sport,
                home_team=home_team,
                away_team=away_team,
                home_abbr=home_abbr,
                away_abbr=away_abbr,
                ordering=ordering,
                game_id=game_id,
            )
        return count

    def _load_moneyline_market(
        self,
        *,
        market: dict,
        event_ticker: str,
        comp_name: str,
        sport: str,
        home_team: str,
        away_team: str,
        home_abbr: str,
        away_abbr: str,
        ordering: str,
        game_id=None,
    ) -> int:
        normalized = normalize_gamma_market_to_clob_format(market)
        market_slug = market.get("slug", "")
        outcomes = self._parse_outcomes(market)
        count = 0
        for token_info in normalized.get("tokens", []):
            token_id = token_info.get("token_id")
            outcome = token_info.get("outcome")
            if not token_id:
                continue
            role, claim = _role_and_claim_for_token(
                market_slug=market_slug,
                event_ticker=event_ticker,
                outcome=outcome,
                ordering=ordering,
                home_abbr=home_abbr,
                away_abbr=away_abbr,
                outcomes=outcomes,
            )
            if not role:
                continue   # 非 moneyline 主市场方向 → 跳过
            # 每 token 传 normalized 的浅拷贝:`parse_polymarket_instrument` 把 market_info 直接设为
            # instrument.info,**多 token 共享同一 dict 会让后写的 role 覆盖前一个**(2-way home/away
            # 都成 away)。拷贝隔离每个 instrument 的 info。
            instrument = parse_polymarket_instrument(
                market_info=dict(normalized),
                token_id=token_id,
                outcome=outcome,
                ts_init=self._clock.timestamp_ns(),
            )
            if isinstance(instrument.info, dict):
                instrument.info.update({
                    "sport": sport,
                    "competition": comp_name,
                    "home_team": home_team,
                    "away_team": away_team,
                    "selection_role": role,
                    "game_id": game_id,   # #60:sports WS 映射键(== gamma event gameId)
                })
                if claim:
                    instrument.info["claim"] = claim
            self.add(instrument)
            count += 1
        return count

    # ── 辅助 ────────────────────────────────────────────────────────
    async def _fetch_json(self, client, url: str, params: dict | None = None):
        try:
            return await fetch_gamma_json(client, url, params)
        except Exception as e:
            self._log.error(f"PM discovery fetch failed {url} params={params}: {e}")
            return None

    @staticmethod
    def _parse_outcomes(market: dict) -> list:
        import json as _json
        raw = market.get("outcomes", "[]")
        if isinstance(raw, str):
            try:
                return _json.loads(raw)
            except (ValueError, TypeError):
                return []
        return list(raw or [])
