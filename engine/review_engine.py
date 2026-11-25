"""
每日赛后自动核对与复盘归因引擎（用户追加核心闭环功能）
核心原则：
1. 自动核对所有已完场、且赛前有预测记录的比赛：
   - 胜平负主推是否命中
   - Top 3 比分是否命中 / 首选比分是否精准命中
   - Brier Score 概率校准误差
   - 红/黄灯冷门预警是否帮用户避开了大坑
2. 科学区分两类错误（防止把足球偶然运气误当成模型错误去胡乱调参）：
   - VARIANCE_LUCK（偶然事件）：早早红牌、射门完全压制却爆冷不进、实际xG与比分严重背离 -> 提醒“稳住别乱改权重！”
   - MODEL_OR_INTEL_MISS（模型或情报失误）：数据与场面都一边倒且赛前情报漏判或修正方向相反 -> 自动沉淀到 tuning_lessons 经验调优库！
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from data_sources.espn_client import fetch_event_summary
from database import (
    add_tuning_lesson,
    get_conn,
    get_latest_prediction,
    get_odds_history,
    now_iso,
    save_review,
)
from engine.predictor import compute_confidence_score


def _calc_brier(ph: float, pd: float, pa: float, actual: str) -> float:
    yh = 1.0 if actual == "H" else 0.0
    yd = 1.0 if actual == "D" else 0.0
    ya = 1.0 if actual == "A" else 0.0
    return round((ph - yh) ** 2 + (pd - yd) ** 2 + (pa - ya) ** 2, 4)


def review_finished_matches(custom_notes: Optional[Dict[str, Dict[str, str]]] = None) -> List[Dict[str, Any]]:
    """
    扫描所有状态为 FINISHED 且有预测记录的比赛，执行赛后核对与原因归因。
    custom_notes 可由 Antigravity Skill 传入 AI 深度复盘结论（覆盖默认规则归因）。
    """
    custom_notes = custom_notes or {}
    with get_conn() as conn:
        finished = [
            dict(r)
            for r in conn.execute(
                """
                SELECT m.*
                FROM matches m
                WHERE m.status = 'FINISHED'
                  AND m.home_score IS NOT NULL
                  AND m.away_score IS NOT NULL
                  AND EXISTS (SELECT 1 FROM predictions p WHERE p.match_id = m.match_id)
                ORDER BY m.kickoff_utc DESC
                LIMIT 60
                """
            ).fetchall()
        ]

    reviewed_list: List[Dict[str, Any]] = []
    for m in finished:
        mid = m["match_id"]
        pred = get_latest_prediction(mid)
        if not pred:
            continue

        hg, ag = int(m["home_score"]), int(m["away_score"])
        actual_score = f"{hg}-{ag}"
        actual_out = "H" if hg > ag else ("D" if hg == ag else "A")

        top3 = json.loads(pred["top3_scores_json"] or "[]")
        top3_scores = [x["score"] for x in top3]

        outcome_hit = 1 if pred["recommended_outcome"] == actual_out else 0
        top3_hit = 1 if actual_score in top3_scores else 0
        exact_top1 = 1 if (top3_scores and actual_score == top3_scores[0]) else 0
        brier = _calc_brier(
            pred["final_prob_home"], pred["final_prob_draw"], pred["final_prob_away"], actual_out
        )

        # 红/黄灯避坑判定：若赛前给了红/黄灯警告，且市场/表面强队确实翻车（或模型没命中但红灯提前拦住了）
        upset_useful = 1 if (pred["traffic_light"] in ("RED", "YELLOW") and outcome_hit == 0) else 0

        # 拉取 ESPN 赛后技术统计与红牌事件，辅助科学归因
        summary = fetch_event_summary(m["league_code"], m.get("espn_event_id") or "")
        red_cards = summary.get("red_cards") or []
        red_note = "；".join(red_cards) if red_cards else "无红牌"
        box_stats = summary.get("box_stats") or {}

        # 尝试提取射门数对比，判断是否属于“场面完全占优却运气爆冷”
        h_shots = _extract_stat_float(box_stats.get(m["home_team"], {}), "totalShots")
        a_shots = _extract_stat_float(box_stats.get(m["away_team"], {}), "totalShots")

        # 判断赛前 AI 情报修正是否帮了倒忙
        base_probs = {"H": pred["base_prob_home"], "D": pred["base_prob_draw"], "A": pred["base_prob_away"]}
        base_rec = max(base_probs, key=base_probs.get)
        intel_flipped = base_rec != pred["recommended_outcome"]

        # 自动归因逻辑
        if outcome_hit == 1:
            attribution = "ACCURATE"
            cause = (
                f"【命中】主推方向正确（实果 {actual_score}）"
                + (f"，且命中 Top 3 比分（{' / '.join(top3_scores)}）！" if top3_hit else f"，比分略偏离 Top 3（{' / '.join(top3_scores)}）。")
            )
            if intel_flipped:
                cause += " 赛前动态情报修正成功扭转方向，证明该情报维度有效！"
            tuning = "保持当前参数与权重，将该场成功逻辑保留为正向样本。"
        else:
            # 未命中时区分“足球偶然事件”还是“模型/情报失误”
            luck_reasons = []
            if red_cards:
                luck_reasons.append(f"比赛出现红牌改变了人数平衡（{red_note}）")
            if h_shots is not None and a_shots is not None:
                if pred["recommended_outcome"] == "H" and h_shots >= a_shots + 8:
                    luck_reasons.append(f"主队场面大幅占优（射门 {int(h_shots)}:{int(a_shots)}）却未能兑现进球")
                elif pred["recommended_outcome"] == "A" and a_shots >= h_shots + 8:
                    luck_reasons.append(f"客队场面大幅占优（射门 {int(a_shots)}:{int(h_shots)}）却未能兑现进球")

            if luck_reasons:
                attribution = "VARIANCE_LUCK"
                cause = (
                    f"【偶然运气偏差】主推 {pred['recommended_outcome']} 未中（实果 {actual_score}），但核心诱因是："
                    + "；".join(luck_reasons)
                    + "。属于足球低比分高方差的正常波动。"
                )
                tuning = "切勿因单场红牌或门柱运气盲目修改模型权重！"
            else:
                attribution = "MODEL_OR_INTEL_MISS"
                parts = [f"【模型/情报误判】主推 {pred['recommended_outcome']} 未中（实果 {actual_score}）。"]
                if intel_flipped and base_rec == actual_out:
                    parts.append("原数理底座其实预测对了，但动态情报修正力度过大导致改错方向！")
                    tuning = "复查该场动态情报加减分，考虑收紧对应维度（如战意/伤停）的修正幅度。"
                    add_tuning_lesson(
                        category="动态情报修正",
                        lesson=f"{m['home_team_cn']} vs {m['away_team_cn']}（{actual_score}）：情报修正把原本正确的数理底座改偏。",
                        suggested_action="检查该场情报来源可信度，避免在缺乏官宣首发时给出接近上限的修正分。",
                    )
                elif pred["traffic_light"] == "RED":
                    parts.append("赛前红绿灯已发出🔴红灯警告（与市场分歧过大），印证市场提前消化了咱未捕捉到的隐性信息。")
                    tuning = "严格遵守红灯场次纪律：凡红灯且无硬首发实锤的场次，只观望不强推。"
                else:
                    parts.append(
                        f"赛前预期进球 {pred['final_home_xg']:.2f}:{pred['final_away_xg']:.2f}，"
                        f"实际比分 {actual_score}，建议复核双方临场首发轮换、战术克制或战意是否被低估。"
                    )
                    tuning = "如连续3场同类型联赛出现主客场进球偏差，微调 Dixon-Coles 时间衰减 xi 或 xG 融合权重。"
                    add_tuning_lesson(
                        category=f"{m['league_name']}模型校准",
                        lesson=f"{m['home_team_cn']} vs {m['away_team_cn']} 预测 {pred['recommended_outcome']} 失误（实果 {actual_score}）。",
                        suggested_action="结合本周累计复盘样本，核对是否低估了多线体能衰减或替补轮换影响。",
                    )
                cause = " ".join(parts)

        # 如果 AI Skill 提供了更深度的手工/联网复盘分析，优先采用
        if mid in custom_notes:
            cn = custom_notes[mid]
            if cn.get("attribution_type"):
                attribution = cn["attribution_type"]
            if cn.get("root_cause_analysis"):
                cause = cn["root_cause_analysis"]
            if cn.get("tuning_suggestion"):
                tuning = cn["tuning_suggestion"]

        # 计算收盘赔率价值 CLV（最终封盘隐含概率 - 早期快照隐含概率，正值说明我们提前抓住了大资金方向）
        clv_diff = None
        odds_hist = get_odds_history(mid)
        if len(odds_hist) >= 2:
            key_map = {"H": "home_implied", "D": "draw_implied", "A": "away_implied"}
            imp_key = key_map.get(pred["recommended_outcome"], "home_implied")
            p_open = odds_hist[0].get(imp_key)
            p_close = odds_hist[-1].get(imp_key)
            if p_open is not None and p_close is not None:
                clv_diff = round((p_close - p_open) * 100.0, 2)

        conf_score = pred.get("confidence_score")
        if conf_score is None:
            conf_score = compute_confidence_score(pred)["score"]

        review_record = {
            "match_id": mid,
            "prediction_id": pred["id"],
            "reviewed_at": now_iso(),
            "actual_home_score": hg,
            "actual_away_score": ag,
            "actual_outcome": actual_out,
            "outcome_hit": outcome_hit,
            "top3_score_hit": top3_hit,
            "exact_top1_hit": exact_top1,
            "brier_score": brier,
            "upset_alert_useful": upset_useful,
            "attribution_type": attribution,
            "actual_xg_home": None,
            "actual_xg_away": None,
            "red_cards_note": red_note,
            "root_cause_analysis": cause,
            "tuning_suggestion": tuning,
            "confidence_score": conf_score,
            "clv_diff": clv_diff,
        }
        save_review(review_record)
        reviewed_list.append({**review_record, "home_team_cn": m["home_team_cn"], "away_team_cn": m["away_team_cn"], "league_name": m["league_name"]})

    return reviewed_list


def _extract_stat_float(stats: Dict[str, str], key: str) -> Optional[float]:
    val = stats.get(key)
    if val is None:
        return None
    try:
        return float(str(val).replace("%", "").strip())
    except ValueError:
        return None


def summarize_review_performance(reviews: List[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总一周实盘复盘战绩指标（含按把握度星级分层命中率 & 平均 CLV 检验）"""
    n = len(reviews)
    if n == 0:
        return {
            "total": 0,
            "outcome_hits": 0,
            "outcome_acc": 0.0,
            "top3_hits": 0,
            "top3_acc": 0.0,
            "exact_top1_hits": 0,
            "avg_brier": 0.0,
            "luck_misses": 0,
            "model_misses": 0,
            "upset_saves": 0,
            "avg_clv": None,
            "tier_stats": {},
        }
    hits = sum(r["outcome_hit"] for r in reviews)
    top3 = sum(r["top3_score_hit"] for r in reviews)
    top1 = sum(r["exact_top1_hit"] for r in reviews)
    brier = round(sum(r["brier_score"] for r in reviews) / n, 4)
    luck = sum(1 for r in reviews if r["attribution_type"] == "VARIANCE_LUCK")
    miss = sum(1 for r in reviews if r["attribution_type"] == "MODEL_OR_INTEL_MISS")
    saves = sum(r.get("upset_alert_useful", 0) for r in reviews)

    clvs = [r["clv_diff"] for r in reviews if r.get("clv_diff") is not None]
    avg_clv = round(sum(clvs) / len(clvs), 2) if clvs else None

    # 按把握度星级分层检验（验证咱们的把握度排序公式是否真有效！）
    buckets = {
        "高把握(≥68分)": [r for r in reviews if (r.get("confidence_score") or 50) >= 68],
        "中把握(55-67分)": [r for r in reviews if 55 <= (r.get("confidence_score") or 50) < 68],
        "低把握/红灯(<55分)": [r for r in reviews if (r.get("confidence_score") or 50) < 55],
    }
    tier_stats = {}
    for label, grp in buckets.items():
        if grp:
            tier_stats[label] = {
                "total": len(grp),
                "hits": sum(x["outcome_hit"] for x in grp),
                "acc": round(sum(x["outcome_hit"] for x in grp) / len(grp), 4),
            }

    # 全玩法长期命中率统计（双选防冷、大小球2.5、绿灯胜平负），方便一周后看哪种玩法最稳
    dc_hits = 0
    o25_hits = 0
    green_total = 0
    green_hits = 0
    for r in reviews:
        hg, ag = int(r["actual_home_score"]), int(r["actual_away_score"])
        ph = float(r.get("final_prob_home") or 0)
        pa = float(r.get("final_prob_away") or 0)
        if ph >= pa:
            dc_hits += 1 if hg >= ag else 0
        else:
            dc_hits += 1 if ag >= hg else 0
        if r.get("traffic_light") == "GREEN":
            green_total += 1
            green_hits += int(r["outcome_hit"])
        # 大小2.5需查 predictions 表中的 over_25_prob（若存在）
        if r.get("over_25_prob") is not None:
            pred_o25 = float(r["over_25_prob"]) >= 0.5
            o25_hits += 1 if (pred_o25 == ((hg + ag) >= 3)) else 0

    return {
        "total": n,
        "outcome_hits": hits,
        "outcome_acc": round(hits / n, 4),
        "double_chance_hits": dc_hits,
        "double_chance_acc": round(dc_hits / n, 4),
        "green_total": green_total,
        "green_hits": green_hits,
        "green_acc": round(green_hits / green_total, 4) if green_total else None,
        "top3_hits": top3,
        "top3_acc": round(top3 / n, 4),
        "exact_top1_hits": top1,
        "avg_brier": brier,
        "luck_misses": luck,
        "model_misses": miss,
        "upset_saves": saves,
        "avg_clv": avg_clv,
        "tier_stats": tier_stats,
    }
