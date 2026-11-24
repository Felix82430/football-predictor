"""
无未来函数历史滚动回测引擎（用于第一周试运营前的底盘验准）
严格按照比赛日期滚动：预测某轮比赛时，模型【只允许看到开赛日前一天的历史数据】，
杜绝任何“事后诸葛亮”作弊，并直接与当时真实主流机构收市赔率做对照。
"""
from __future__ import annotations

import json
import math
from datetime import timedelta
from typing import Any, Dict, List

import pandas as pd

from config import LEAGUES, load_settings
from data_sources.football_data_client import load_league_history
from data_sources.team_names import to_cn_name
from database import now_iso, save_backtest_run
from engine.dixon_coles import DixonColesModel


def _brier_score(probs: tuple[float, float, float], actual: str) -> float:
    y = (1.0 if actual == "H" else 0.0, 1.0 if actual == "D" else 0.0, 1.0 if actual == "A" else 0.0)
    return sum((p - yi) ** 2 for p, yi in zip(probs, y))


def _log_loss(probs: tuple[float, float, float], actual: str) -> float:
    idx = {"H": 0, "D": 1, "A": 2}[actual]
    p = max(1e-6, min(1.0 - 1e-6, probs[idx]))
    return -math.log(p)


def run_rolling_backtest(
    leagues: List[str] | None = None,
    lookback_days: int = 45,
) -> Dict[str, Any]:
    """
    对五大联赛最近 lookback_days 天的已完场比赛执行严格滚动回测。
    每 7 天重新拟合一次模型（兼顾速度与真实每周更新节奏）。
    """
    settings = load_settings()
    xi = float(settings.get("dixon_coles_xi", 0.0065))
    target_leagues = leagues or ["EPL", "LALIGA", "SERIEA", "BUNDESLIGA", "LIGUE1"]

    all_records: List[Dict[str, Any]] = []
    league_summaries: Dict[str, Dict[str, Any]] = {}
    date_min_seen = None
    date_max_seen = None

    for lg in target_leagues:
        df = load_league_history(lg, include_prev_season=True)
        if df.empty or len(df) < 80:
            continue
        max_date = df["Date"].max()
        start_test_date = max_date - timedelta(days=lookback_days)
        test_df = df[df["Date"] >= start_test_date].copy()
        if test_df.empty:
            continue

        if date_min_seen is None or test_df["Date"].min() < date_min_seen:
            date_min_seen = test_df["Date"].min()
        if date_max_seen is None or test_df["Date"].max() > date_max_seen:
            date_max_seen = test_df["Date"].max()

        # 按周分批重训，严禁偷看未来数据
        test_df["week_bucket"] = test_df["Date"].dt.to_period("W")
        lg_hits = 0
        lg_top3 = 0
        lg_total = 0
        lg_brier = 0.0

        for _, week_grp in test_df.groupby("week_bucket"):
            cutoff = week_grp["Date"].min()
            train_df = df[df["Date"] < cutoff]
            if len(train_df) < 60:
                continue
            model = DixonColesModel(xi=xi).fit(train_df, ref_date=cutoff)
            if not model.is_fitted:
                continue

            for _, row in week_grp.iterrows():
                pred = model.predict(row["HomeTeam"], row["AwayTeam"])
                if not pred:
                    continue
                hg, ag = int(row["FTHG"]), int(row["FTAG"])
                actual_score = f"{hg}-{ag}"
                actual_out = "H" if hg > ag else ("D" if hg == ag else "A")

                rec_out = pred["recommended_outcome"]
                top3_list = [x["score"] for x in pred["top3_scores"]]
                hit = 1 if rec_out == actual_out else 0
                top3_hit = 1 if actual_score in top3_list else 0
                probs = (pred["prob_home"], pred["prob_draw"], pred["prob_away"])
                brier = _brier_score(probs, actual_out)
                ll = _log_loss(probs, actual_out)

                # 对比当时市场赔率主推方向
                mkt_hit = None
                if pd.notna(row.get("OddsH")) and pd.notna(row.get("OddsD")) and pd.notna(row.get("OddsA")):
                    oh, od, oa = float(row["OddsH"]), float(row["OddsD"]), float(row["OddsA"])
                    mkt_rec = min([("H", oh), ("D", od), ("A", oa)], key=lambda x: x[1])[0]
                    mkt_hit = 1 if mkt_rec == actual_out else 0

                lg_hits += hit
                lg_top3 += top3_hit
                lg_total += 1
                lg_brier += brier

                all_records.append(
                    {
                        "date": row["Date"].strftime("%Y-%m-%d"),
                        "league_code": lg,
                        "league_name": LEAGUES[lg]["name_zh"],
                        "home_team": to_cn_name(row["HomeTeam"]),
                        "away_team": to_cn_name(row["AwayTeam"]),
                        "pred_outcome": rec_out,
                        "actual_outcome": actual_out,
                        "actual_score": actual_score,
                        "top3_scores": top3_list,
                        "prob_home": pred["prob_home"],
                        "prob_draw": pred["prob_draw"],
                        "prob_away": pred["prob_away"],
                        "hit": hit,
                        "top3_hit": top3_hit,
                        "mkt_hit": mkt_hit,
                        "brier": round(brier, 4),
                        "log_loss": round(ll, 4),
                    }
                )

        if lg_total > 0:
            league_summaries[lg] = {
                "league_name": LEAGUES[lg]["name_zh"],
                "total": lg_total,
                "outcome_acc": round(lg_hits / lg_total, 4),
                "top3_acc": round(lg_top3 / lg_total, 4),
                "brier": round(lg_brier / lg_total, 4),
            }

    total = len(all_records)
    if total == 0:
        return {"error": "暂无足够历史数据完成回测"}

    outcome_acc = round(sum(r["hit"] for r in all_records) / total, 4)
    top3_acc = round(sum(r["top3_hit"] for r in all_records) / total, 4)
    avg_brier = round(sum(r["brier"] for r in all_records) / total, 4)
    avg_ll = round(sum(r["log_loss"] for r in all_records) / total, 4)

    mkt_rows = [r["mkt_hit"] for r in all_records if r["mkt_hit"] is not None]
    mkt_acc = round(sum(mkt_rows) / len(mkt_rows), 4) if mkt_rows else None

    date_from = date_min_seen.strftime("%Y-%m-%d") if date_min_seen else ""
    date_to = date_max_seen.strftime("%Y-%m-%d") if date_max_seen else ""

    details = {
        "league_summaries": league_summaries,
        "recent_matches": sorted(all_records, key=lambda x: x["date"], reverse=True)[:40],
    }

    run_record = {
        "created_at": now_iso(),
        "league_scope": ",".join(target_leagues),
        "date_from": date_from,
        "date_to": date_to,
        "total_matches": total,
        "outcome_accuracy": outcome_acc,
        "top3_score_accuracy": top3_acc,
        "brier_score": avg_brier,
        "log_loss": avg_ll,
        "market_accuracy": mkt_acc,
        "details_json": json.dumps(details, ensure_ascii=False),
    }
    save_backtest_run(run_record)
    return {**run_record, "details": details}
