"""
Football-Data.co.uk 免费历史赛果、赔率与裁判统计抓取模块（带本地缓存）
参考开源项目 soccerdata / penaltyblog 的数据源标准。
用于：
1. 训练 Dixon-Coles 数理底座（近两个赛季五大联赛完整赛果）
2. 计算全欧球队实时 ELO 战力与休息天数
3. 计算主裁判历史场均红黄牌/严厉度档案（驱动异常裁判触发器）
4. 提供无未来函数的真实历史回测数据集（含当时主流机构收市赔率 B365/Avg/PS）
"""
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import pandas as pd

from config import CACHE_DIR, CURRENT_SEASON_FD, LEAGUES, PREV_SEASON_FD
from data_sources.team_names import normalize_team_name

FD_BASE = "https://www.football-data.co.uk/mmz4281"


def _fetch_csv(season_code: str, fd_code: str, max_age_hours: float = 6.0) -> pd.DataFrame:
    cache_file = CACHE_DIR / f"fd_{season_code}_{fd_code}.csv"
    if cache_file.exists():
        age_hours = (time.time() - cache_file.stat().st_mtime) / 3600.0
        if age_hours < max_age_hours:
            try:
                return pd.read_csv(cache_file, encoding="utf-8-sig")
            except Exception:
                pass

    url = f"{FD_BASE}/{season_code}/{fd_code}.csv"
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200 and len(resp.text) > 200:
                cache_file.write_text(resp.text, encoding="utf-8-sig")
                return pd.read_csv(io.StringIO(resp.text))
    except Exception:
        if cache_file.exists():
            try:
                return pd.read_csv(cache_file, encoding="utf-8-sig")
            except Exception:
                pass
    return pd.DataFrame()


def load_league_history(
    league_code: str,
    include_prev_season: bool = True,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """加载指定联赛的历史完场赛果表（标准化列名与队名）"""
    meta = LEAGUES.get(league_code)
    if not meta or not meta.get("fd_code"):
        return pd.DataFrame()

    fd_code = meta["fd_code"]
    max_age = 0.0 if force_refresh else 6.0
    frames: List[pd.DataFrame] = []

    if include_prev_season:
        df_prev = _fetch_csv(PREV_SEASON_FD, fd_code, max_age_hours=72.0)
        if not df_prev.empty:
            df_prev = df_prev.copy()
            df_prev["Season"] = PREV_SEASON_FD
            frames.append(df_prev)

    df_curr = _fetch_csv(CURRENT_SEASON_FD, fd_code, max_age_hours=max_age)
    if not df_curr.empty:
        df_curr = df_curr.copy()
        df_curr["Season"] = CURRENT_SEASON_FD
        frames.append(df_curr)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    if "HomeTeam" not in df.columns or "FTHG" not in df.columns:
        return pd.DataFrame()

    df = df.dropna(subset=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]).copy()
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["Date"])

    df["League"] = league_code
    df["HomeTeam"] = df["HomeTeam"].astype(str).apply(normalize_team_name)
    df["AwayTeam"] = df["AwayTeam"].astype(str).apply(normalize_team_name)
    df["FTHG"] = pd.to_numeric(df["FTHG"], errors="coerce")
    df["FTAG"] = pd.to_numeric(df["FTAG"], errors="coerce")
    df = df.dropna(subset=["FTHG", "FTAG"])

    # 提取最佳可用市场欧赔（优先 Pinnacle PS > 市场均值 Avg > Bet365 B365）
    def _pick_odds(row: pd.Series, suffix: str) -> Optional[float]:
        for prefix in ("PS", "Avg", "B365"):
            col = f"{prefix}{suffix}"
            if col in row and pd.notna(row[col]):
                try:
                    v = float(row[col])
                    if v > 1.01:
                        return v
                except (TypeError, ValueError):
                    pass
        return None

    df["OddsH"] = df.apply(lambda r: _pick_odds(r, "H"), axis=1)
    df["OddsD"] = df.apply(lambda r: _pick_odds(r, "D"), axis=1)
    df["OddsA"] = df.apply(lambda r: _pick_odds(r, "A"), axis=1)

    for col in ("HY", "AY", "HR", "AR", "HF", "AF", "HS", "AS", "HST", "AST"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        else:
            df[col] = 0.0

    if "Referee" not in df.columns:
        df["Referee"] = ""
    df["Referee"] = df["Referee"].fillna("").astype(str).str.strip()

    df = df.sort_values("Date").reset_index(drop=True)
    return df


# 欧战常客所在的欧洲二线免费联赛（0成本，专门解决欧冠跨联赛球队实力评估盲区）
EURO_SECONDARY_LEAGUES: Dict[str, str] = {
    "EREDIVISIE": "N1",   # 荷甲（埃因霍温/费耶诺德/阿贾克斯）
    "PRIMEIRA": "P1",     # 葡超（波尔图/本菲卡/葡萄牙体育）
    "BELGIUM": "B1",      # 比甲（布鲁日/圣吉罗斯）
    "TURKEY": "T1",       # 土超（加拉塔萨雷/费内巴切）
    "SCOTLAND": "SC0",    # 苏超（凯尔特人/流浪者）
    "GREECE": "G1",       # 希超（雅典AEK/奥林匹亚科斯）
}


def load_secondary_league_history(league_key: str, fd_code: str, force_refresh: bool = False) -> pd.DataFrame:
    """加载欧洲二线联赛历史赛果（带 24 小时本地缓存，0 费用且不拖慢云端）"""
    max_age = 0.0 if force_refresh else 24.0
    frames: List[pd.DataFrame] = []
    df_prev = _fetch_csv(PREV_SEASON_FD, fd_code, max_age_hours=168.0)
    if not df_prev.empty:
        df_prev = df_prev.copy()
        df_prev["Season"] = PREV_SEASON_FD
        frames.append(df_prev)
    df_curr = _fetch_csv(CURRENT_SEASON_FD, fd_code, max_age_hours=max_age)
    if not df_curr.empty:
        df_curr = df_curr.copy()
        df_curr["Season"] = CURRENT_SEASON_FD
        frames.append(df_curr)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "HomeTeam" not in df.columns or "FTHG" not in df.columns:
        return pd.DataFrame()
    df = df.dropna(subset=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]).copy()
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["Date"])
    df["League"] = league_key
    df["HomeTeam"] = df["HomeTeam"].astype(str).apply(normalize_team_name)
    df["AwayTeam"] = df["AwayTeam"].astype(str).apply(normalize_team_name)
    df["FTHG"] = pd.to_numeric(df["FTHG"], errors="coerce")
    df["FTAG"] = pd.to_numeric(df["FTAG"], errors="coerce")
    df = df.dropna(subset=["FTHG", "FTAG"])
    return df[["Date", "League", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "Season"]].sort_values("Date").reset_index(drop=True)


def load_all_top5_history(force_refresh: bool = False, include_euro_secondary: bool = False) -> pd.DataFrame:
    """加载五大联赛全部历史数据（可选附带荷甲/葡超/比甲/土超/苏超/希超用于全欧 ELO 与欧冠建模）"""
    dfs = []
    for code in ("EPL", "LALIGA", "SERIEA", "BUNDESLIGA", "LIGUE1"):
        df = load_league_history(code, include_prev_season=True, force_refresh=force_refresh)
        if not df.empty:
            dfs.append(df)
    if include_euro_secondary:
        for k, fd_code in EURO_SECONDARY_LEAGUES.items():
            sdf = load_secondary_league_history(k, fd_code, force_refresh=force_refresh)
            if not sdf.empty:
                dfs.append(sdf)
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True).sort_values("Date").reset_index(drop=True)


def build_referee_profiles(df: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    """
    基于历史执法记录构建主裁判严厉度档案（目前英超等含完整 Referee 字段）。
    返回每位裁判的执法场次、场均黄牌、场均红牌、严厉度等级。
    """
    if df.empty or "Referee" not in df.columns:
        return {}
    valid = df[df["Referee"].str.len() > 2].copy()
    if valid.empty:
        return {}

    valid["total_yellow"] = valid["HY"] + valid["AY"]
    valid["total_red"] = valid["HR"] + valid["AR"]
    valid["total_fouls"] = valid["HF"] + valid["AF"]

    league_avg_y = float(valid["total_yellow"].mean())
    league_avg_r = float(valid["total_red"].mean())

    profiles: Dict[str, Dict[str, float]] = {}
    for ref, grp in valid.groupby("Referee"):
        n = len(grp)
        if n < 5:
            continue
        avg_y = float(grp["total_yellow"].mean())
        avg_r = float(grp["total_red"].mean())
        avg_f = float(grp["total_fouls"].mean())
        # 计算相对严厉指数（>1.20 严厉哨，<0.82 宽松哨）
        strictness = (avg_y / max(league_avg_y, 1.0)) * 0.7 + (
            (avg_r + 0.05) / max(league_avg_r + 0.05, 0.1)
        ) * 0.3
        profiles[ref.lower()] = {
            "name": ref,
            "matches": n,
            "avg_yellow": round(avg_y, 2),
            "avg_red": round(avg_r, 3),
            "avg_fouls": round(avg_f, 1),
            "strictness_index": round(strictness, 2),
            "league_avg_yellow": round(league_avg_y, 2),
        }
    return profiles
