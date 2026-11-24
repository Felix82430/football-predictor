"""
三层混合预测编排引擎（核心大脑）
第 1 层：70% 客观数理底盘（Dixon-Coles 模型 + 预期进球 xG 融合 + 实时 ELO）
第 2 层：30% 动态情报修正（自动接入多线体能/极端天气/异常裁判触发器 + AI 情报官封顶加减分明细账）
第 3 层：市场赔率去水校准 + 盘口走势快照分析 + 🟢🟡🔴红绿灯体检
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pandas as pd

from config import (
    INTEL_LIMITS,
    MARKET_TRAFFIC_LIGHT,
    TOTAL_INTEL_CAP,
    load_settings,
)
from data_sources.espn_client import fetch_event_summary
from data_sources.football_data_client import (
    build_referee_profiles,
    load_all_top5_history,
    load_league_history,
)
from data_sources.understat_client import estimate_xg_from_shots, fetch_league_team_xg
from data_sources.weather_client import fetch_match_weather
from database import (
    get_conn,
    get_odds_history,
    now_iso,
    save_prediction,
)
from engine.article_generator import generate_media_article
from engine.dixon_coles import DixonColesModel, build_score_matrix
from engine.elo_rating import compute_elo_ratings, compute_rest_and_fatigue, elo_to_expected_goals


class LeagueEngineCache:
    """缓存当前进程内的各联赛历史表、拟合好的 Dixon-Coles 模型、xG 与裁判档案，避免重复计算"""

    def __init__(self) -> None:
        self.all_df: Optional[pd.DataFrame] = None
        self.elo_map: Dict[str, float] = {}
        self.ref_profiles: Dict[str, Dict[str, float]] = {}
        self.league_models: Dict[str, DixonColesModel] = {}
        self.xg_maps: Dict[str, Dict[str, Dict[str, float]]] = {}
        self.proxy_xg: Dict[str, Dict[str, float]] = {}

    def ensure_loaded(self, force_refresh: bool = False) -> None:
        if self.all_df is not None and not force_refresh:
            return
        settings = load_settings()
        xi = float(settings.get("dixon_coles_xi", 0.0065))

        self.all_df = load_all_top5_history(force_refresh=force_refresh)
        self.elo_map = compute_elo_ratings(self.all_df)
        self.ref_profiles = build_referee_profiles(self.all_df)
        self.proxy_xg = estimate_xg_from_shots(self.all_df)

        for lg in ("EPL", "LALIGA", "SERIEA", "BUNDESLIGA", "LIGUE1"):
            sub = self.all_df[self.all_df["League"] == lg] if not self.all_df.empty else pd.DataFrame()
            if len(sub) >= 30:
                self.league_models[lg] = DixonColesModel(xi=xi).fit(sub)
            xg = fetch_league_team_xg(lg)
            if xg:
                self.xg_maps[lg] = xg

    def get_team_xg(self, league_code: str, team: str) -> Optional[Dict[str, float]]:
        if league_code in self.xg_maps and team in self.xg_maps[league_code]:
            return self.xg_maps[league_code][team]
        for lg_xg in self.xg_maps.values():
            if team in lg_xg:
                return lg_xg[team]
        return self.proxy_xg.get(team)


ENGINE_CACHE = LeagueEngineCache()


def evaluate_referee_trigger(referee_name: str, ref_profiles: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
    """异常裁判触发器：平时严格为0，仅遇到极端严厉哨/宽松哨时预警与微调"""
    if not referee_name:
        return {
            "triggered": False,
            "referee": "待公布",
            "home_delta": 0.0,
            "away_delta": 0.0,
            "summary": "主裁判尚未公布，修正为0",
        }

    key = referee_name.strip().lower()
    prof = ref_profiles.get(key)
    if not prof:
        # 尝试姓氏模糊匹配（如 ESPN 的 'Michael Oliver' vs Football-Data 的 'M Oliver'）
        parts = key.split()
        if len(parts) >= 2:
            short_key = f"{parts[0][0]} {parts[-1]}"
            prof = ref_profiles.get(short_key)
            if not prof:
                for k, v in ref_profiles.items():
                    if k.endswith(" " + parts[-1]) or parts[-1] == k:
                        prof = v
                        break

    if not prof:
        return {
            "triggered": False,
            "referee": referee_name,
            "home_delta": 0.0,
            "away_delta": 0.0,
            "summary": f"主裁判：{referee_name}（历史执法样本不足，默认中性不修正）",
        }

    idx = prof["strictness_index"]
    if idx >= 1.22:
        return {
            "triggered": True,
            "referee": referee_name,
            "profile": prof,
            "home_delta": 0.015,
            "away_delta": -0.015,
            "summary": (
                f"主裁判：{referee_name}【严厉哨触发】场均黄牌 {prof['avg_yellow']} 张"
                f"（联赛均值 {prof['league_avg_yellow']}），场均红牌 {prof['avg_red']} 张，"
                f"红点风险偏高，客队激进防守易吃亏"
            ),
        }
    if idx <= 0.80:
        return {
            "triggered": True,
            "referee": referee_name,
            "profile": prof,
            "home_delta": 0.01,
            "away_delta": 0.01,
            "summary": (
                f"主裁判：{referee_name}【宽松哨触发】场均黄牌仅 {prof['avg_yellow']} 张，"
                f"比赛流畅度高、对抗容忍度大，利于双方快节奏进攻"
            ),
        }

    return {
        "triggered": False,
        "referee": referee_name,
        "profile": prof,
        "home_delta": 0.0,
        "away_delta": 0.0,
        "summary": (
            f"主裁判：{referee_name}（正常尺度：场均黄牌 {prof['avg_yellow']} 张，不触发额外修正）"
        ),
    }


def analyze_odds_trend(odds_history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """分析本地定时抓拍的盘口走势（初盘 vs 最新即时盘）"""
    if not odds_history:
        return {
            "market_prob_home": None,
            "market_prob_draw": None,
            "market_prob_away": None,
            "trend_note": "暂无公开赔率快照（等待下次自动抓拍）",
            "abnormal_shift": False,
        }

    first = odds_history[0]
    latest = odds_history[-1]
    mh = latest.get("home_implied")
    md = latest.get("draw_implied")
    ma = latest.get("away_implied")

    if len(odds_history) == 1:
        note = (
            f"当前初盘参考（{latest['source']}）：主胜 {latest['home_odds']} / 平 {latest['draw_odds']} / 客胜 {latest['away_odds']}"
            + (f" | 盘口 {latest['asian_handicap']}" if latest.get("asian_handicap") else "")
            + "（已记录首帧快照，持续跟踪后续临场变化）"
        )
        return {
            "market_prob_home": mh,
            "market_prob_draw": md,
            "market_prob_away": ma,
            "trend_note": note,
            "abnormal_shift": False,
        }

    dh = round((mh - first["home_implied"]) * 100, 1)
    da = round((ma - first["away_implied"]) * 100, 1)
    abnormal = abs(dh) >= 5.0 or abs(da) >= 5.0

    shift_parts = []
    if abs(dh) >= 2.0:
        shift_parts.append(f"主胜隐含概率{'上升' if dh > 0 else '下降'} {abs(dh):.1f}%")
    if abs(da) >= 2.0:
        shift_parts.append(f"客胜隐含概率{'上升' if da > 0 else '下降'} {abs(da):.1f}%")

    trend_core = "，".join(shift_parts) if shift_parts else "盘口走势平稳，无明显资金异动"
    prefix = "⚠️【异常变盘预警】" if abnormal else "盘口走势："
    note = (
        f"{prefix}{trend_core}（初盘 {first['home_odds']}/{first['draw_odds']}/{first['away_odds']}"
        f" → 即时 {latest['home_odds']}/{latest['draw_odds']}/{latest['away_odds']}，共 {len(odds_history)} 次快照）"
    )
    return {
        "market_prob_home": mh,
        "market_prob_draw": md,
        "market_prob_away": ma,
        "trend_note": note,
        "abnormal_shift": abnormal,
    }


def compute_traffic_light(
    final_probs: tuple[float, float, float],
    market_probs: tuple[Optional[float], Optional[float], Optional[float]],
    has_strong_intel: bool,
    abnormal_odds_shift: bool,
) -> tuple[str, str]:
    """第三层：市场赔率红绿灯防偏体检"""
    if any(x is None for x in market_probs):
        return "YELLOW", "暂无主流机构赔率对照，当前预测完全基于数理底盘与情报，建议开赛前核对盘口。"

    diffs = [abs(f - m) for f, m in zip(final_probs, market_probs)]  # type: ignore
    max_diff = max(diffs)
    diff_pct = round(max_diff * 100, 1)

    if abnormal_odds_shift and max_diff > MARKET_TRAFFIC_LIGHT["green_max_diff"]:
        return "RED", f"盘口出现异常大幅波动且与模型分歧达 {diff_pct}%，极可能存在未公开伤停或大资金异动，强烈建议临场看首发后再决定！"

    if max_diff <= MARKET_TRAFFIC_LIGHT["green_max_diff"]:
        return "GREEN", f"模型概率与市场去水概率高度一致（最大偏差仅 {diff_pct}%），属于稳健共振场次。"

    if max_diff <= MARKET_TRAFFIC_LIGHT["yellow_max_diff"]:
        if has_strong_intel:
            return "YELLOW", f"模型与市场存在 {diff_pct}% 偏差，但已有明确伤停/赛程情报支撑，具备潜在价值或防冷参考意义。"
        return "YELLOW", f"模型与市场存在 {diff_pct}% 偏差，当前尚未查到重磅新闻解释，建议重点关注临场首发。"

    return "RED", f"模型与市场分歧过大（高达 {diff_pct}%）！可能存在模型未捕捉的隐性战意或临场突发伤病，严禁盲目重注，请务必人工复核。"


def clamp_intel_items(raw_items: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], float, float]:
    """
    强制执行咱们约定的【动态情报封顶规则】：
    每项严格限制在 INTEL_LIMITS[category].max_abs 内，单队总修正不超过 ±25%。
    """
    cleaned: List[Dict[str, Any]] = []
    sum_h = 0.0
    sum_a = 0.0

    for item in raw_items:
        cat = item.get("category", "injuries_lineup")
        meta = INTEL_LIMITS.get(cat, {"label": cat, "max_abs": 0.10})
        cap = float(meta["max_abs"])

        hd = float(item.get("home_delta", 0.0))
        ad = float(item.get("away_delta", 0.0))
        hd_clamped = round(max(-cap, min(cap, hd)), 4)
        ad_clamped = round(max(-cap, min(cap, ad)), 4)

        sum_h += hd_clamped
        sum_a += ad_clamped

        cleaned.append(
            {
                "category": cat,
                "label": meta["label"],
                "home_delta": hd_clamped,
                "away_delta": ad_clamped,
                "reason": str(item.get("reason") or "").strip(),
                "source_url": str(item.get("source_url") or "").strip(),
                "source_name": str(item.get("source_name") or "公开情报").strip(),
            }
        )

    total_h = round(max(-TOTAL_INTEL_CAP, min(TOTAL_INTEL_CAP, sum_h)), 4)
    total_a = round(max(-TOTAL_INTEL_CAP, min(TOTAL_INTEL_CAP, sum_a)), 4)
    return cleaned, total_h, total_a


def predict_match(
    match: Dict[str, Any],
    ai_intel_items: Optional[List[Dict[str, Any]]] = None,
    stage: str = "PREMATCH",
    lock: bool = False,
    fetch_live_summary: bool = True,
) -> Dict[str, Any]:
    """
    执行单场完整三层预测并存入数据库。
    ai_intel_items 可由 Antigravity Skill（方案B）联网核查后传入结构化加减分明细。
    """
    ENGINE_CACHE.ensure_loaded()
    settings = load_settings()
    xg_blend = float(settings.get("xg_blend_weight", 0.35))

    league_code = match["league_code"]
    home = match["home_team"]
    away = match["away_team"]

    # 如果还没有裁判信息且允许拉详情，顺手向 ESPN 拉一次（临场还能拿官方首发）
    referee_name = match.get("referee") or ""
    live_summary: Dict[str, Any] = {}
    if fetch_live_summary and match.get("espn_event_id"):
        live_summary = fetch_event_summary(league_code, match["espn_event_id"])
        if not referee_name and live_summary.get("referee"):
            referee_name = live_summary["referee"]

    # ========== 第 1 层：70% 客观数理底盘 ==========
    lam_h, lam_a, rho = None, None, -0.06
    dc_model = ENGINE_CACHE.league_models.get(league_code)
    if dc_model and dc_model.is_fitted:
        eg = dc_model.predict_expected_goals(home, away)
        if eg:
            lam_h, lam_a = eg
            rho = dc_model.rho

    # 欧冠跨联赛或升班马未命中单联赛模型时，用全欧 ELO 平滑估算
    home_elo = ENGINE_CACHE.elo_map.get(home, 1580.0)
    away_elo = ENGINE_CACHE.elo_map.get(away, 1580.0)
    if lam_h is None or lam_a is None:
        lam_h, lam_a = elo_to_expected_goals(home_elo, away_elo)

    # 融合预期进球（Understat xG / 射门拟合 xG），戳穿“纯靠运气连胜”假象
    h_xg_info = ENGINE_CACHE.get_team_xg(league_code, home)
    a_xg_info = ENGINE_CACHE.get_team_xg(league_code, away)
    if h_xg_info and a_xg_info:
        xg_lam_h = 0.5 * (h_xg_info["home_xg_for"] + a_xg_info["away_xg_against"])
        xg_lam_a = 0.5 * (a_xg_info["away_xg_for"] + h_xg_info["home_xg_against"])
        # 结合近6场 xG 动量
        xg_lam_h = 0.65 * xg_lam_h + 0.35 * (0.5 * (h_xg_info["recent6_xg_for"] + a_xg_info["recent6_xg_against"]))
        xg_lam_a = 0.65 * xg_lam_a + 0.35 * (0.5 * (a_xg_info["recent6_xg_for"] + h_xg_info["recent6_xg_against"]))

        lam_h = round((1.0 - xg_blend) * lam_h + xg_blend * xg_lam_h, 3)
        lam_a = round((1.0 - xg_blend) * lam_a + xg_blend * xg_lam_a, 3)

    base_matrix = build_score_matrix(lam_h, lam_a, rho=rho)

    # ========== 第 2 层：动态情报修正（自动触发器 + AI 情报官账单） ==========
    # 1) 多线体能与休息差（自动计算）
    with get_conn() as conn:
        recent_db = [dict(r) for r in conn.execute("SELECT home_team, away_team, kickoff_utc, status FROM matches WHERE status='FINISHED'").fetchall()]
    fatigue_info = compute_rest_and_fatigue(
        home, away, match["kickoff_utc"], ENGINE_CACHE.all_df, recent_db
    )

    # 2) Open-Meteo 极端天气战术触发器
    weather_info = fetch_match_weather(home, away, league_code, match["kickoff_utc"])

    # 3) 异常裁判触发器
    referee_info = evaluate_referee_trigger(referee_name, ENGINE_CACHE.ref_profiles)

    # 组装明细账单（保留外部 AI 情报官传入的伤停/战意/换帅项 + 补充自动触发项）
    combined_items: List[Dict[str, Any]] = list(ai_intel_items or [])
    existing_cats = {x.get("category") for x in combined_items}

    if "fixture_fatigue" not in existing_cats and (
        fatigue_info["auto_home_delta"] != 0.0 or fatigue_info["auto_away_delta"] != 0.0
    ):
        combined_items.append(
            {
                "category": "fixture_fatigue",
                "home_delta": fatigue_info["auto_home_delta"],
                "away_delta": fatigue_info["auto_away_delta"],
                "reason": fatigue_info["summary"],
                "source_name": "自动赛程体能计算器",
                "source_url": "",
            }
        )

    if "weather_referee" not in existing_cats and (
        weather_info.get("triggered") or referee_info.get("triggered")
    ):
        wr_h = float(weather_info.get("home_delta", 0.0)) + float(referee_info.get("home_delta", 0.0))
        wr_a = float(weather_info.get("away_delta", 0.0)) + float(referee_info.get("away_delta", 0.0))
        parts = []
        if weather_info.get("triggered"):
            parts.append(weather_info["summary"])
        if referee_info.get("triggered"):
            parts.append(referee_info["summary"])
        combined_items.append(
            {
                "category": "weather_referee",
                "home_delta": wr_h,
                "away_delta": wr_a,
                "reason": "；".join(parts),
                "source_name": "Open-Meteo气象 + 历史裁判档案",
                "source_url": "",
            }
        )

    cleaned_items, total_h_delta, total_a_delta = clamp_intel_items(combined_items)

    final_lam_h = round(max(0.20, lam_h * (1.0 + total_h_delta)), 3)
    final_lam_a = round(max(0.20, lam_a * (1.0 + total_a_delta)), 3)
    final_matrix = build_score_matrix(final_lam_h, final_lam_a, rho=rho)

    # ========== 第 3 层：盘口走势 + 红绿灯体检 ==========
    odds_history = get_odds_history(match["match_id"])
    odds_analysis = analyze_odds_trend(odds_history)
    has_strong_intel = abs(total_h_delta) >= 0.05 or abs(total_a_delta) >= 0.05

    traffic_light, traffic_note = compute_traffic_light(
        (final_matrix["prob_home"], final_matrix["prob_draw"], final_matrix["prob_away"]),
        (
            odds_analysis["market_prob_home"],
            odds_analysis["market_prob_draw"],
            odds_analysis["market_prob_away"],
        ),
        has_strong_intel=has_strong_intel,
        abnormal_odds_shift=odds_analysis["abnormal_shift"],
    )

    outcome_zh = {"H": f"主胜（{match['home_team_cn']}）", "D": "平局", "A": f"客胜（{match['away_team_cn']}）"}[
        final_matrix["recommended_outcome"]
    ]
    top3_str = " / ".join(f"{x['score']}({x['prob']*100:.1f}%)" for x in final_matrix["top3_scores"])
    summary_text = (
        f"主推方向：{outcome_zh} | 胜平负概率 {final_matrix['prob_home']*100:.1f}% / "
        f"{final_matrix['prob_draw']*100:.1f}% / {final_matrix['prob_away']*100:.1f}% | "
        f"Top3比分：{top3_str} | 预期进球 {final_lam_h:.2f} : {final_lam_a:.2f}"
    )

    pred_record: Dict[str, Any] = {
        "match_id": match["match_id"],
        "stage": stage,
        "is_locked": 1 if lock else 0,
        "model_version": settings.get("model_version", "v1.0-试运营"),
        "created_at": now_iso(),
        "base_home_xg": lam_h,
        "base_away_xg": lam_a,
        "base_prob_home": base_matrix["prob_home"],
        "base_prob_draw": base_matrix["prob_draw"],
        "base_prob_away": base_matrix["prob_away"],
        "intel_items_json": json.dumps(cleaned_items, ensure_ascii=False),
        "home_intel_delta": total_h_delta,
        "away_intel_delta": total_a_delta,
        "weather_json": json.dumps(weather_info, ensure_ascii=False),
        "referee_json": json.dumps(referee_info, ensure_ascii=False),
        "fatigue_json": json.dumps(fatigue_info, ensure_ascii=False),
        "final_home_xg": final_lam_h,
        "final_away_xg": final_lam_a,
        "final_prob_home": final_matrix["prob_home"],
        "final_prob_draw": final_matrix["prob_draw"],
        "final_prob_away": final_matrix["prob_away"],
        "recommended_outcome": final_matrix["recommended_outcome"],
        "top3_scores_json": json.dumps(final_matrix["top3_scores"], ensure_ascii=False),
        "over_25_prob": final_matrix["over_25_prob"],
        "btts_prob": final_matrix["btts_prob"],
        "market_prob_home": odds_analysis["market_prob_home"],
        "market_prob_draw": odds_analysis["market_prob_draw"],
        "market_prob_away": odds_analysis["market_prob_away"],
        "traffic_light": traffic_light,
        "traffic_note": traffic_note,
        "odds_trend_note": odds_analysis["trend_note"],
        "summary_text": summary_text,
        "media_article": "",
        "pushed_feishu": 0,
    }

    # 自动生成自媒体初稿
    pred_record["media_article"] = generate_media_article(
        match=match,
        pred=pred_record,
        home_elo=home_elo,
        away_elo=away_elo,
        starters=live_summary.get("starters") or {},
    )

    pred_id = save_prediction(pred_record)
    pred_record["id"] = pred_id
    pred_record["home_elo"] = home_elo
    pred_record["away_elo"] = away_elo
    pred_record["starters"] = live_summary.get("starters") or {}
    return pred_record
