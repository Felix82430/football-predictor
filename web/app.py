"""
FastAPI 本地网页版后台服务
提供赛程同步、盘口抓拍、单场/批量预测、情报加减分微调、实盘锁单、
无未来函数历史回测、每日赛后核对复盘、飞书卡片推送等全部接口。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from config import INTEL_LIMITS, LEAGUES, load_settings, save_settings
from data_sources.espn_client import fetch_league_scoreboard
from database import (
    get_conn,
    get_latest_backtest,
    get_latest_prediction,
    get_match_detail,
    init_db,
    insert_odds_snapshot,
    list_matches_with_predictions,
    list_reviews,
    list_tuning_lessons,
    lock_prediction,
    mark_feishu_pushed,
    upsert_match,
)
from engine.backtest import run_rolling_backtest
from engine.predictor import ENGINE_CACHE, compute_confidence_score, predict_match, save_manual_intel
from engine.review_engine import review_finished_matches, summarize_review_performance
from engine.scheduler import SCHEDULER
from notify.feishu_bot import (
    build_daily_brief_card,
    build_review_report_card,
    build_single_final_card,
    send_feishu_card,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="足球比分量化情报与复盘系统", version="1.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()
    SCHEDULER.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    SCHEDULER.stop()


class IntelItemIn(BaseModel):
    category: str
    home_delta: float = 0.0
    away_delta: float = 0.0
    reason: str = ""
    source_name: str = "手动/AI核查"
    source_url: str = ""


class AnalyzeRequest(BaseModel):
    stage: str = "PREMATCH"
    lock: bool = False
    intel_items: List[IntelItemIn] = []


class SettingsIn(BaseModel):
    feishu_webhook: Optional[str] = None
    feishu_secret: Optional[str] = None
    dixon_coles_xi: Optional[float] = None
    xg_blend_weight: Optional[float] = None
    auto_push_enabled: Optional[bool] = None
    daily_push_times: Optional[List[str]] = None
    review_push_time: Optional[str] = None
    prematch_1h_enabled: Optional[bool] = None


class BacktestIn(BaseModel):
    lookback_days: int = 45
    leagues: Optional[List[str]] = None


@app.get("/", response_class=HTMLResponse)
def index() -> Any:
    html_file = STATIC_DIR / "index.html"
    return FileResponse(str(html_file))


@app.get("/api/meta")
def get_meta() -> Dict[str, Any]:
    return {
        "leagues": LEAGUES,
        "intel_limits": INTEL_LIMITS,
        "settings": load_settings(),
    }


@app.get("/api/overview")
def get_overview() -> Dict[str, Any]:
    with get_conn() as conn:
        total_matches = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        upcoming = conn.execute("SELECT COUNT(*) FROM matches WHERE status='SCHEDULED'").fetchone()[0]
        live_cnt = conn.execute("SELECT COUNT(*) FROM matches WHERE status='LIVE'").fetchone()[0]
        finished_cnt = conn.execute("SELECT COUNT(*) FROM matches WHERE status='FINISHED'").fetchone()[0]
        locked_preds = conn.execute("SELECT COUNT(DISTINCT match_id) FROM predictions WHERE is_locked=1").fetchone()[0]
        odds_snaps = conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]

    reviews = list_reviews(limit=200)
    review_summary = summarize_review_performance(reviews)
    latest_bt = get_latest_backtest()
    if latest_bt and latest_bt.get("details_json"):
        latest_bt["details"] = json.loads(latest_bt["details_json"])

    return {
        "total_matches": total_matches,
        "upcoming_matches": upcoming,
        "live_matches": live_cnt,
        "finished_matches": finished_cnt,
        "started_matches": live_cnt + finished_cnt,
        "locked_predictions": locked_preds,
        "odds_snapshots": odds_snaps,
        "review_summary": review_summary,
        "latest_backtest": latest_bt,
    }


@app.post("/api/sync")
def sync_all_leagues(days_back: int = 3, days_ahead: int = 7) -> Dict[str, Any]:
    """同步五大联赛+欧冠赛程与完场比分，并抓拍最新盘口快照"""
    settings = load_settings()
    enabled = settings.get("enabled_leagues") or list(LEAGUES.keys())
    synced = 0
    snaps = 0
    per_league: Dict[str, int] = {}

    for code in enabled:
        matches = fetch_league_scoreboard(code, days_back=days_back, days_ahead=days_ahead)
        per_league[code] = len(matches)
        for m in matches:
            upsert_match(m)
            synced += 1
            odds = m.get("odds") or {}
            # 若数据库尚无该场快照且有初盘 open，先写入初盘基准点，再写当前即时盘
            if odds.get("open_home_odds") and odds.get("open_draw_odds") and odds.get("open_away_odds"):
                with get_conn() as conn:
                    cnt = conn.execute("SELECT COUNT(*) FROM odds_snapshots WHERE match_id=?", (m["match_id"],)).fetchone()[0]
                if cnt == 0:
                    sid0 = insert_odds_snapshot(
                        match_id=m["match_id"],
                        source=f"{odds.get('source', 'ESPN')}(初盘)",
                        home_odds=odds["open_home_odds"],
                        draw_odds=odds["open_draw_odds"],
                        away_odds=odds["open_away_odds"],
                        asian_handicap=odds.get("asian_handicap"),
                        over_under_line=odds.get("over_under_line"),
                    )
                    if sid0:
                        snaps += 1
            if odds.get("home_odds") and odds.get("draw_odds") and odds.get("away_odds"):
                sid = insert_odds_snapshot(
                    match_id=m["match_id"],
                    source=odds.get("source", "ESPN"),
                    home_odds=odds["home_odds"],
                    draw_odds=odds["draw_odds"],
                    away_odds=odds["away_odds"],
                    asian_handicap=odds.get("asian_handicap"),
                    over_under_line=odds.get("over_under_line"),
                    over_odds=odds.get("over_odds"),
                    under_odds=odds.get("under_odds"),
                )
                if sid:
                    snaps += 1

    return {"ok": True, "synced_matches": synced, "new_odds_snapshots": snaps, "per_league": per_league}


@app.get("/api/matches")
def api_list_matches(
    league: str = "ALL",
    status: str = "ALL",
    sort: str = "confidence",
    limit: int = 80,
) -> Dict[str, Any]:
    rows = list_matches_with_predictions(league_code=league, status=status, limit=limit)
    for r in rows:
        if r.get("top3_scores_json"):
            try:
                r["top3_scores"] = json.loads(r["top3_scores_json"])
            except Exception:
                r["top3_scores"] = []
        else:
            r["top3_scores"] = []

        if r.get("prediction_id"):
            pred = get_latest_prediction(r["match_id"])
            r["confidence"] = compute_confidence_score(pred) if pred else None
        else:
            r["confidence"] = None

    if sort == "confidence":
        rows.sort(
            key=lambda x: (
                1 if x.get("confidence") else 0,
                (x.get("confidence") or {}).get("score", -1),
            ),
            reverse=True,
        )
    return {"items": rows}


@app.get("/api/matches/{match_id}")
def api_match_detail(match_id: str) -> Dict[str, Any]:
    detail = get_match_detail(match_id)
    if not detail:
        raise HTTPException(status_code=404, detail="未找到该场比赛")
    pred = detail.get("prediction")
    if pred:
        for field in (
            "intel_items_json",
            "weather_json",
            "referee_json",
            "fatigue_json",
            "top3_scores_json",
            "lottery_json",
            "value_ev_json",
            "news_injuries_json",
        ):
            key = field.replace("_json", "")
            try:
                pred[key] = json.loads(pred.get(field) or ("[]" if "items" in field or "scores" in field else "{}"))
            except Exception:
                pred[key] = [] if ("items" in field or "scores" in field) else {}
    return detail


@app.post("/api/analyze/{match_id}")
def api_analyze_match(match_id: str, req: AnalyzeRequest) -> Dict[str, Any]:
    detail = get_match_detail(match_id)
    if not detail:
        raise HTTPException(status_code=404, detail="未找到该场比赛")
    items = [x.model_dump() for x in req.intel_items]
    # 同步存入纯文本防冲突情报库（仅保存人工/AI手填项，不含自动天气/体能项）
    manual_only = [
        x for x in items if x.get("source_name") not in ("自动赛程体能计算器", "Open-Meteo气象 + 历史裁判档案")
    ]
    save_manual_intel(match_id, manual_only)
    pred = predict_match(
        match=detail["match"],
        ai_intel_items=manual_only,
        stage=req.stage,
        lock=req.lock,
        fetch_live_summary=True,
    )
    return {"ok": True, "prediction": pred}


@app.post("/api/analyze-upcoming")
def api_analyze_upcoming(limit: int = 25) -> Dict[str, Any]:
    """批量对未开赛焦点场次跑第1+2层自动初判模型"""
    ENGINE_CACHE.ensure_loaded()
    rows = list_matches_with_predictions(status="SCHEDULED", limit=limit)
    count = 0
    for r in rows:
        predict_match(match=r, ai_intel_items=[], stage="PREMATCH", lock=False, fetch_live_summary=False)
        count += 1
    return {"ok": True, "analyzed_count": count}


@app.post("/api/predictions/{pred_id}/lock")
def api_lock_prediction(pred_id: int) -> Dict[str, Any]:
    lock_prediction(pred_id)
    return {"ok": True, "prediction_id": pred_id}


@app.post("/api/backtest")
def api_run_backtest(req: BacktestIn) -> Dict[str, Any]:
    res = run_rolling_backtest(leagues=req.leagues, lookback_days=req.lookback_days)
    if "error" in res:
        raise HTTPException(status_code=400, detail=res["error"])
    return {"ok": True, "result": res}


@app.get("/api/backtest/latest")
def api_latest_backtest() -> Dict[str, Any]:
    bt = get_latest_backtest()
    if not bt:
        return {"result": None}
    bt["details"] = json.loads(bt.get("details_json") or "{}")
    return {"result": bt}


@app.post("/api/review/run")
def api_run_review() -> Dict[str, Any]:
    """先同步最新完场比分，再执行每日赛后自动核对与偏差归因"""
    sync_all_leagues(days_back=5, days_ahead=3)
    reviewed = review_finished_matches()
    all_reviews = list_reviews(limit=100)
    summary = summarize_review_performance(all_reviews)
    return {"ok": True, "newly_reviewed": len(reviewed), "summary": summary, "reviews": all_reviews}


@app.get("/api/reviews")
def api_get_reviews() -> Dict[str, Any]:
    reviews = list_reviews(limit=100)
    for r in reviews:
        try:
            r["top3_scores"] = json.loads(r.get("top3_scores_json") or "[]")
        except Exception:
            r["top3_scores"] = []
    return {
        "summary": summarize_review_performance(reviews),
        "reviews": reviews,
        "lessons": list_tuning_lessons(limit=50),
    }


@app.get("/api/settings")
def api_get_settings() -> Dict[str, Any]:
    s = load_settings()
    s["scheduler_status"] = SCHEDULER.status()
    return s


@app.post("/api/settings")
def api_save_settings(req: SettingsIn) -> Dict[str, Any]:
    payload = {k: v for k, v in req.model_dump().items() if v is not None}
    updated = save_settings(payload)
    return {"ok": True, "settings": updated}


@app.post("/api/feishu/test")
def api_feishu_test() -> Dict[str, Any]:
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "⚽ 飞书机器人连接测试成功"}, "template": "green"},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "你的**足球比分量化情报与复盘系统**已成功连通飞书！\n后续每日初判简报、临场终审预警与赛后复盘报告都将直接推到本群。",
                },
            }
        ],
    }
    return send_feishu_card(card)


@app.post("/api/feishu/push-daily")
def api_feishu_push_daily() -> Dict[str, Any]:
    rows = list_matches_with_predictions(status="SCHEDULED", limit=10)
    bundle = []
    for r in rows:
        if r.get("prediction_id"):
            pred = get_latest_prediction(r["match_id"])
            if pred:
                bundle.append({"match": r, "prediction": pred})
    if not bundle:
        return {"ok": False, "error": "当前暂无已分析的未开赛场次，请先点击【一键初判】"}
    card = build_daily_brief_card(bundle, title_suffix="今日焦点赛前初判简报")
    res = send_feishu_card(card)
    if res.get("ok"):
        for item in bundle:
            mark_feishu_pushed(item["prediction"]["id"])
    return res


@app.post("/api/feishu/push-match/{match_id}")
def api_feishu_push_match(match_id: str) -> Dict[str, Any]:
    detail = get_match_detail(match_id)
    if not detail or not detail.get("prediction"):
        return {"ok": False, "error": "该场尚未生成预测报告，请先执行分析"}
    card = build_single_final_card(detail["match"], detail["prediction"])
    res = send_feishu_card(card)
    if res.get("ok"):
        mark_feishu_pushed(detail["prediction"]["id"])
    return res


@app.post("/api/feishu/push-review")
def api_feishu_push_review() -> Dict[str, Any]:
    reviews = list_reviews(limit=20)
    if not reviews:
        return {"ok": False, "error": "暂无赛后复盘记录"}
    summary = summarize_review_performance(reviews)
    card = build_review_report_card(summary, reviews)
    return send_feishu_card(card)
