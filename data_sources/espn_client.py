"""
免费 ESPN 足球公开接口客户端（无需 API Key）
覆盖：五大联赛 + 欧冠
负责获取：
1. 未来赛程与近期完场比分（支持赛后自动核对复盘）
2. 球场名称、主裁判（临场/赛前公布时）、赔率快照（初盘/即时盘）
3. 单场深度摘要（首发/伤停摘要/近期战绩/红黄牌事件，用于临场终审与赛后归因）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from config import LEAGUES
from data_sources.team_names import normalize_team_name, to_cn_name

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
BJT = timezone(timedelta(hours=8))


def _american_to_decimal(odds_val: Any) -> Optional[float]:
    """将美式赔率（如 -150, +240）或小数赔率统一转为欧赔小数制"""
    if odds_val is None:
        return None
    if isinstance(odds_val, str):
        s = odds_val.strip().upper()
        if s in ("EVEN", "EV"):
            return 2.0
        try:
            num = float(s)
        except ValueError:
            return None
    else:
        try:
            num = float(odds_val)
        except (TypeError, ValueError):
            return None

    # 如果已经是欧赔范围（1.01 ~ 50）
    if 1.01 <= num <= 50.0:
        return round(num, 3)
    # 美式赔率转换
    if num >= 100:
        return round(1.0 + num / 100.0, 3)
    if num <= -100:
        return round(1.0 + 100.0 / abs(num), 3)
    return None


def _parse_espn_odds(comp: Dict[str, Any]) -> Dict[str, Any]:
    """从 ESPN competition 节点精准提取欧赔（含初盘 open 与即时盘 close）、亚盘与大小球线"""
    odds_list = comp.get("odds") or []
    if not odds_list:
        return {}
    first = odds_list[0] or {}
    provider = (first.get("provider") or {}).get("name") or "ESPN-Market"
    ml = first.get("moneyline") or {}

    # 优先读取 moneyline 的即时盘 close，并兼容旧结构 homeTeamOdds
    home_odds = _american_to_decimal((ml.get("home") or {}).get("close", {}).get("odds")) or _american_to_decimal(
        (first.get("homeTeamOdds") or {}).get("moneyLine")
    )
    away_odds = _american_to_decimal((ml.get("away") or {}).get("close", {}).get("odds")) or _american_to_decimal(
        (first.get("awayTeamOdds") or {}).get("moneyLine")
    )
    draw_odds = _american_to_decimal((ml.get("draw") or {}).get("close", {}).get("odds")) or _american_to_decimal(
        (first.get("drawOdds") or {}).get("moneyLine")
    )

    # 同步读取机构官方初盘 open（实现0成本首帧即见盘口走势！）
    open_home = _american_to_decimal((ml.get("home") or {}).get("open", {}).get("odds"))
    open_draw = _american_to_decimal((ml.get("draw") or {}).get("open", {}).get("odds"))
    open_away = _american_to_decimal((ml.get("away") or {}).get("open", {}).get("odds"))

    # 同步读取大小球双边水位（v1.6 修复：结合大/小球赔率反推真实总进球期望，不再直接盲信盘口线）
    total_node = first.get("total") or {}
    over_odds = _american_to_decimal((total_node.get("over") or {}).get("close", {}).get("odds"))
    under_odds = _american_to_decimal((total_node.get("under") or {}).get("close", {}).get("odds"))

    return {
        "source": provider,
        "home_odds": home_odds,
        "draw_odds": draw_odds,
        "away_odds": away_odds,
        "open_home_odds": open_home,
        "open_draw_odds": open_draw,
        "open_away_odds": open_away,
        "asian_handicap": first.get("details") or "",
        "over_under_line": first.get("overUnder"),
        "over_odds": over_odds,
        "under_odds": under_odds,
    }


def _map_status(state: str) -> str:
    s = (state or "").lower()
    if s == "post":
        return "FINISHED"
    if s == "in":
        return "LIVE"
    return "SCHEDULED"


def fetch_league_scoreboard(
    league_code: str,
    days_back: int = 3,
    days_ahead: int = 7,
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """拉取指定联赛过去 days_back 天至未来 days_ahead 天的比赛列表（按单日查询去重，兼容 ESPN 规则）"""
    meta = LEAGUES.get(league_code)
    if not meta:
        return []
    slug = meta["espn_slug"]
    now_utc = datetime.now(timezone.utc)

    urls: List[str] = [f"{ESPN_BASE}/{slug}/scoreboard"]
    for offset in range(-days_back, days_ahead + 1):
        d_str = (now_utc + timedelta(days=offset)).strftime("%Y%m%d")
        urls.append(f"{ESPN_BASE}/{slug}/scoreboard?dates={d_str}")

    seen_ids: set[str] = set()
    events_raw: List[Dict[str, Any]] = []
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            for url in urls:
                try:
                    resp = client.get(url)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    for ev in data.get("events") or []:
                        eid = str(ev.get("id") or "")
                        if eid and eid not in seen_ids:
                            seen_ids.add(eid)
                            events_raw.append(ev)
                except Exception:
                    continue
    except Exception:
        return []

    results: List[Dict[str, Any]] = []
    for ev in events_raw:
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        competitors = comp.get("competitors") or []
        home_node = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away_node = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home_node or not away_node:
            continue

        raw_home = (home_node.get("team") or {}).get("displayName") or ""
        raw_away = (away_node.get("team") or {}).get("displayName") or ""
        home_team = normalize_team_name(raw_home)
        away_team = normalize_team_name(raw_away)

        date_str = ev.get("date") or comp.get("date") or ""
        try:
            dt_utc = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            kickoff_utc = dt_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            kickoff_bjt = dt_utc.astimezone(BJT).strftime("%Y-%m-%d %H:%M")
        except Exception:
            kickoff_utc = date_str
            kickoff_bjt = date_str

        status_type = (comp.get("status") or {}).get("type") or {}
        status = _map_status(status_type.get("state") or "")

        def _parse_int(val: Any) -> Optional[int]:
            try:
                return int(val)
            except (TypeError, ValueError):
                return None

        home_score = _parse_int(home_node.get("score")) if status in ("FINISHED", "LIVE") else None
        away_score = _parse_int(away_node.get("score")) if status in ("FINISHED", "LIVE") else None

        venue_name = (comp.get("venue") or {}).get("fullName") or ""
        officials = comp.get("officials") or []
        referee = officials[0].get("displayName", "") if officials else ""
        odds_info = _parse_espn_odds(comp)

        espn_id = str(ev.get("id") or "")
        match_id = f"{league_code}_{espn_id}"

        results.append(
            {
                "match_id": match_id,
                "league_code": league_code,
                "league_name": meta["name_zh"],
                "home_team": home_team,
                "away_team": away_team,
                "home_team_cn": to_cn_name(home_team),
                "away_team_cn": to_cn_name(away_team),
                "kickoff_utc": kickoff_utc,
                "kickoff_bjt": kickoff_bjt,
                "status": status,
                "home_score": home_score,
                "away_score": away_score,
                "venue": venue_name,
                "referee": referee,
                "espn_event_id": espn_id,
                "odds": odds_info,
                "raw_json": {
                    "espn_id": espn_id,
                    "raw_home": raw_home,
                    "raw_away": raw_away,
                    "home_form": home_node.get("form") or "",
                    "away_form": away_node.get("form") or "",
                    "status_detail": status_type.get("shortDetail") or "",
                },
            }
        )
    return results


def fetch_event_summary(league_code: str, espn_event_id: str, timeout: float = 12.0) -> Dict[str, Any]:
    """拉取单场比赛详情（裁判、官方首发、红黄牌、赛后射门/控球等，用于临场终审与赛后归因）"""
    meta = LEAGUES.get(league_code)
    if not meta or not espn_event_id:
        return {}
    url = f"{ESPN_BASE}/{meta['espn_slug']}/summary?event={espn_event_id}"
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return {}

    game_info = data.get("gameInfo") or {}
    officials = game_info.get("officials") or []
    referee = officials[0].get("displayName", "") if officials else ""

    # 解析红牌事件（用于赛后复盘区分“红牌偶然运气偏差”）
    red_cards: List[str] = []
    for ev in data.get("keyEvents") or []:
        ev_type = ((ev.get("type") or {}).get("text") or "").lower()
        if "red card" in ev_type:
            clock = (ev.get("clock") or {}).get("displayValue") or ""
            text = ev.get("text") or ev_type
            red_cards.append(f"{clock} {text}".strip())

    # 提取官方首发（赛前1小时左右放出）及阵容中的伤病/停赛状态
    rosters_out: Dict[str, List[str]] = {}
    injury_notes: List[str] = []
    for roster in data.get("rosters") or []:
        team_name = normalize_team_name((roster.get("team") or {}).get("displayName") or "")
        team_cn = to_cn_name(team_name)
        starters = []
        for p in roster.get("roster") or []:
            ath = p.get("athlete") or {}
            pname = ath.get("displayName") or ""
            if p.get("starter") and pname:
                starters.append(pname)
            # 某些比赛节点附带伤停状态
            inj_status = p.get("injuryStatus") or (ath.get("injuries") or [{}])[0].get("status") if isinstance(ath.get("injuries"), list) and ath.get("injuries") else ""
            if inj_status and pname:
                injury_notes.append(f"{team_cn}·{pname}({inj_status})")
        if starters:
            rosters_out[team_name] = starters

    # 提取顶层 injuries 数组（若 ESPN 提供）
    for team_inj in data.get("injuries") or []:
        tname = to_cn_name(normalize_team_name((team_inj.get("team") or {}).get("displayName") or ""))
        for item in (team_inj.get("injuries") or [])[:6]:
            ath_name = (item.get("athlete") or {}).get("displayName") or ""
            status_txt = item.get("status") or item.get("shortComment") or "伤停"
            if ath_name:
                injury_notes.append(f"{tname}·{ath_name}({status_txt})")

    # 提取免费赛前新闻快讯头条（自动筛选含伤病/停赛/轮换/赛前关键词的标题）
    news_headlines: List[Dict[str, str]] = []
    for art in ((data.get("news") or {}).get("articles") or [])[:5]:
        hl = (art.get("headline") or "").strip()
        desc = (art.get("description") or "").strip()
        link = ((art.get("links") or {}).get("web") or {}).get("href") or ""
        if hl:
            news_headlines.append({"headline": hl, "description": desc[:140], "url": link})

    # 提取近期 5 场真实战绩摘要（ESPN lastFiveGames，跨联赛欧冠同样完整！）
    last_five: Dict[str, str] = {}
    for entry in data.get("lastFiveGames") or []:
        tname = normalize_team_name((entry.get("team") or {}).get("displayName") or "")
        evs = entry.get("events") or []
        seq = []
        for e in evs[:5]:
            res_code = (e.get("gameResult") or "").upper()
            if res_code in ("W", "D", "L"):
                seq.append({"W": "胜", "D": "平", "L": "负"}[res_code])
        if tname and seq:
            last_five[tname] = "".join(seq)

    # 提取赛后统计（控球/射门/射正）
    box_stats: Dict[str, Dict[str, str]] = {}
    for team_box in (data.get("boxscore") or {}).get("teams") or []:
        tname = normalize_team_name((team_box.get("team") or {}).get("displayName") or "")
        stats_map = {}
        for st in team_box.get("statistics") or []:
            stats_map[st.get("name") or st.get("label") or ""] = str(st.get("displayValue") or "")
        box_stats[tname] = stats_map

    return {
        "referee": referee,
        "red_cards": red_cards,
        "starters": rosters_out,
        "injuries": injury_notes[:12],
        "news_headlines": news_headlines[:3],
        "last_five": last_five,
        "box_stats": box_stats,
    }
