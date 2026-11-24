"""
全欧球队实时 ELO 战力评分与多线体能负荷计算模块
0 网络延迟替代超时的 ClubElo 接口，基于五大联赛近两赛季完整赛果动态演算，
并结合欧冠与各队最近赛程精确计算“休息天数差”和“近14天密集赛程场次”。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

# 联赛基础实力偏移（用于欧冠跨联赛对战时的初值微调，参考 UEFA 系数与 ClubElo 均值）
LEAGUE_BASE_ELO: Dict[str, float] = {
    "EPL": 1660.0,
    "LALIGA": 1620.0,
    "SERIEA": 1610.0,
    "BUNDESLIGA": 1605.0,
    "LIGUE1": 1560.0,
    "UCL": 1630.0,
}


def compute_elo_ratings(df: pd.DataFrame, k_factor: float = 24.0, home_adv: float = 65.0) -> Dict[str, float]:
    """
    基于历史赛果表计算每支球队的最新 ELO 战力分。
    引入净胜球乘数（Goal Difference Multiplier），大胜强队加分更合理。
    """
    if df.empty:
        return {}

    ratings: Dict[str, float] = {}
    ordered = df.sort_values("Date")

    for _, row in ordered.iterrows():
        h = str(row["HomeTeam"])
        a = str(row["AwayTeam"])
        lg = str(row.get("League", "EPL"))
        base = LEAGUE_BASE_ELO.get(lg, 1550.0)

        rh = ratings.get(h, base)
        ra = ratings.get(a, base)

        hg = int(row["FTHG"])
        ag = int(row["FTAG"])

        expected_h = 1.0 / (1.0 + 10.0 ** ((ra - (rh + home_adv)) / 400.0))
        if hg > ag:
            actual_h = 1.0
        elif hg == ag:
            actual_h = 0.5
        else:
            actual_h = 0.0

        gd = abs(hg - ag)
        gd_mult = 1.0 if gd <= 1 else (1.5 if gd == 2 else (1.75 + (gd - 3) / 8.0))
        delta = k_factor * gd_mult * (actual_h - expected_h)

        ratings[h] = rh + delta
        ratings[a] = ra - delta

    return {k: round(v, 1) for k, v in ratings.items()}


def compute_rest_and_fatigue(
    home_team: str,
    away_team: str,
    kickoff_utc: str,
    history_df: pd.DataFrame,
    recent_matches_db: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    自动计算主客队在开球前的休息天数与近14天比赛密集度（多线作战体能指标）。
    融合国内联赛历史表 + 数据库中的欧冠/近期所有赛事。
    """
    try:
        kickoff_dt = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return {
            "home_rest_days": None,
            "away_rest_days": None,
            "home_games_14d": 0,
            "away_games_14d": 0,
            "rest_diff_days": 0.0,
            "auto_home_delta": 0.0,
            "auto_away_delta": 0.0,
            "summary": "开球时间异常，无法计算休息差",
        }

    def _collect_team_dates(team: str) -> List[datetime]:
        dates: List[datetime] = []
        if not history_df.empty:
            sub = history_df[(history_df["HomeTeam"] == team) | (history_df["AwayTeam"] == team)]
            for dt_val in sub["Date"].dropna():
                dt_utc = pd.Timestamp(dt_val).to_pydatetime().replace(tzinfo=timezone.utc)
                if dt_utc < kickoff_dt:
                    dates.append(dt_utc)
        if recent_matches_db:
            for m in recent_matches_db:
                if m.get("home_team") == team or m.get("away_team") == team:
                    if m.get("status") != "FINISHED":
                        continue
                    try:
                        mdt = datetime.fromisoformat(m["kickoff_utc"].replace("Z", "+00:00")).astimezone(timezone.utc)
                        if mdt < kickoff_dt:
                            dates.append(mdt)
                    except Exception:
                        pass
        # 按日期去重（防止同一天重复记录）
        unique_days = {}
        for d in dates:
            unique_days[d.strftime("%Y-%m-%d")] = d
        return sorted(unique_days.values())

    def _stats(team: str):
        dts = _collect_team_dates(team)
        if not dts:
            return None, 0
        last_dt = dts[-1]
        rest_days = round((kickoff_dt - last_dt).total_seconds() / 86400.0, 1)
        games_14d = sum(1 for d in dts if 0 < (kickoff_dt - d).total_seconds() <= 14 * 86400)
        return rest_days, games_14d

    h_rest, h_14d = _stats(home_team)
    a_rest, a_14d = _stats(away_team)

    auto_h = 0.0
    auto_a = 0.0
    notes = []

    # 72小时恢复红线：休息 <= 3.2 天且近14天 >= 3场，自动触发轻度体能疲劳修正（仍留空间给AI结合轮换情况微调）
    if h_rest is not None and h_rest <= 3.2:
        auto_h -= 0.04 if h_14d >= 3 else 0.025
        notes.append(f"主队仅休{h_rest}天（近14天{h_14d}战），跌破72小时恢复红线")
    if a_rest is not None and a_rest <= 3.2:
        auto_a -= 0.04 if a_14d >= 3 else 0.025
        notes.append(f"客队仅休{a_rest}天（近14天{a_14d}战），跌破72小时恢复红线")

    rest_diff = 0.0
    if h_rest is not None and a_rest is not None:
        rest_diff = round(h_rest - a_rest, 1)
        if rest_diff >= 2.5 and a_rest <= 4.5:
            auto_a -= 0.025
            notes.append(f"主队比客队多休{rest_diff}天，体能占优")
        elif rest_diff <= -2.5 and h_rest <= 4.5:
            auto_h -= 0.025
            notes.append(f"客队比主队多休{abs(rest_diff)}天，主队赛程吃亏")

    auto_h = round(max(-0.10, min(0.10, auto_h)), 4)
    auto_a = round(max(-0.10, min(0.10, auto_a)), 4)

    summary = (
        f"主队休息 {h_rest if h_rest is not None else '未知'} 天（近14天{h_14d}战） vs "
        f"客队休息 {a_rest if a_rest is not None else '未知'} 天（近14天{a_14d}战）"
    )
    if notes:
        summary += " | " + "；".join(notes)

    return {
        "home_rest_days": h_rest,
        "away_rest_days": a_rest,
        "home_games_14d": h_14d,
        "away_games_14d": a_14d,
        "rest_diff_days": rest_diff,
        "auto_home_delta": auto_h,
        "auto_away_delta": auto_a,
        "summary": summary,
    }


def elo_to_expected_goals(
    home_elo: float,
    away_elo: float,
    league_avg_goals: float = 2.75,
    home_adv_elo: float = 65.0,
) -> tuple[float, float]:
    """当欧冠遇到非五大联赛球队（无完整联赛CSV训练样本）时，通过 ELO 差值平滑映射基准预期进球"""
    diff = (home_elo + home_adv_elo) - away_elo
    exp_share = 1.0 / (1.0 + 10.0 ** (-diff / 400.0))
    # 将胜率份额映射为合理的攻防进球预期
    total = league_avg_goals * (1.0 + 0.08 * abs(diff) / 300.0)
    lam_h = max(0.35, min(3.8, total * (0.18 + 0.64 * exp_share)))
    lam_a = max(0.25, min(3.5, total * (0.18 + 0.64 * (1.0 - exp_share))))
    return round(lam_h, 3), round(lam_a, 3)
