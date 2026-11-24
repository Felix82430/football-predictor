"""
Understat 免费预期进球（xG / xGA / npxG）抓取模块（带本地缓存与容错降级）
参考开源项目 soccerdata 的 Understat 解析方式。
如果 Understat 临时改版或限流，系统自动降级使用 Football-Data 射门/射正拟合 xG，绝不崩盘！
"""
from __future__ import annotations

import json
import re
import time
from typing import Dict, Any

import httpx
import pandas as pd

from config import CACHE_DIR, CURRENT_SEASON_UNDERSTAT, LEAGUES
from data_sources.team_names import normalize_team_name


def _decode_understat_json(html: str, var_name: str) -> Any:
    pattern = rf"var\s+{var_name}\s*=\s*JSON\.parse\('([^']+)'\)"
    m = re.search(pattern, html)
    if not m:
        return None
    raw = m.group(1)
    decoded = raw.encode("utf-8").decode("unicode_escape")
    return json.loads(decoded)


def fetch_league_team_xg(league_code: str, max_age_hours: float = 12.0) -> Dict[str, Dict[str, float]]:
    """
    获取五大联赛各队近阶段预期进球（xG）与预期失球（xGA）汇总指标。
    返回：{ "Arsenal": {"xg_for_avg": 2.05, "xg_against_avg": 0.85, "recent5_xg_for": 2.2, ...} }
    """
    meta = LEAGUES.get(league_code)
    if not meta or not meta.get("understat_slug"):
        return {}

    slug = meta["understat_slug"]
    cache_file = CACHE_DIR / f"understat_{slug}_{CURRENT_SEASON_UNDERSTAT}.json"
    if cache_file.exists():
        age_hours = (time.time() - cache_file.stat().st_mtime) / 3600.0
        if age_hours < max_age_hours:
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:
                pass

    url = f"https://understat.com/league/{slug}/{CURRENT_SEASON_UNDERSTAT}"
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                return {}
            teams_data = _decode_understat_json(resp.text, "teamsData")
    except Exception:
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    if not isinstance(teams_data, dict):
        return {}

    result: Dict[str, Dict[str, float]] = {}
    for _, tinfo in teams_data.items():
        raw_title = tinfo.get("title") or ""
        team = normalize_team_name(raw_title)
        history = tinfo.get("history") or []
        if not history:
            continue

        n = len(history)
        recent = history[-6:]
        home_games = [g for g in history if g.get("h_a") == "h"]
        away_games = [g for g in history if g.get("h_a") == "a"]

        def _mean(arr, key):
            vals = [float(x.get(key, 0.0)) for x in arr]
            return round(sum(vals) / len(vals), 3) if vals else 1.25

        result[team] = {
            "matches": n,
            "xg_for_avg": _mean(history, "xG"),
            "xg_against_avg": _mean(history, "xGA"),
            "npxg_for_avg": _mean(history, "npxG"),
            "npxg_against_avg": _mean(history, "npxGA"),
            "recent6_xg_for": _mean(recent, "xG"),
            "recent6_xg_against": _mean(recent, "xGA"),
            "home_xg_for": _mean(home_games, "xG"),
            "home_xg_against": _mean(home_games, "xGA"),
            "away_xg_for": _mean(away_games, "xG"),
            "away_xg_against": _mean(away_games, "xGA"),
            "source": "Understat",
        }

    if result:
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def estimate_xg_from_shots(df: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    """
    兜底方案：当 Understat 暂不可用或欧冠非五大联赛球队缺失 xG 时，
    利用射门（HS/AS）和射正（HST/AST）按经验系数估算预期进球 proxy-xG。
    经验公式：proxy_xG ≈ 0.28 * 射正 + 0.04 * (总射门 - 射正)
    """
    if df.empty:
        return {}
    stats: Dict[str, Dict[str, list]] = {}
    for _, row in df.tail(1200).iterrows():
        h, a = row["HomeTeam"], row["AwayTeam"]
        h_s, h_st = float(row.get("HS", 0)), float(row.get("HST", 0))
        a_s, a_st = float(row.get("AS", 0)), float(row.get("AST", 0))
        if h_s + a_s <= 0:
            h_xg, a_xg = float(row["FTHG"]), float(row["FTAG"])
        else:
            h_xg = 0.28 * h_st + 0.04 * max(h_s - h_st, 0.0)
            a_xg = 0.28 * a_st + 0.04 * max(a_s - a_st, 0.0)

        for t, fg, ag, is_home in ((h, h_xg, a_xg, True), (a, a_xg, h_xg, False)):
            d = stats.setdefault(t, {"f": [], "a": [], "hf": [], "ha": [], "af": [], "aa": []})
            d["f"].append(fg)
            d["a"].append(ag)
            if is_home:
                d["hf"].append(fg)
                d["ha"].append(ag)
            else:
                d["af"].append(fg)
                d["aa"].append(ag)

    out: Dict[str, Dict[str, float]] = {}
    for t, d in stats.items():
        def _avg(lst):
            return round(sum(lst) / len(lst), 3) if lst else 1.25
        out[t] = {
            "matches": len(d["f"]),
            "xg_for_avg": _avg(d["f"][-15:]),
            "xg_against_avg": _avg(d["a"][-15:]),
            "recent6_xg_for": _avg(d["f"][-6:]),
            "recent6_xg_against": _avg(d["a"][-6:]),
            "home_xg_for": _avg(d["hf"][-8:]),
            "home_xg_against": _avg(d["ha"][-8:]),
            "away_xg_for": _avg(d["af"][-8:]),
            "away_xg_against": _avg(d["aa"][-8:]),
            "source": "ShotProxy",
        }
    return out
