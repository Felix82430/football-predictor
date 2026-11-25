"""
GitHub Actions 0成本云端定时任务入口脚本
即使本地电脑彻底关机，云端依然按北京时间准点自动推送到手机飞书：
- daily_noon    : 每日 12:00 午间初判简报
- daily_evening : 每日 20:00 晚间盘口更新简报
- prematch_1h   : 赛前 1 小时窗口精准巡检（拉取官方首发 + 最终临场盘口 + 锁单推送）
- daily_review  : 次日 09:00~09:30 自动核对昨晚完场比分 + 归因复盘推送
- auto          : 根据当前北京时间自动匹配应执行的任务（供统一 cron 调用）
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from config import LEAGUES, load_settings
from data_sources.espn_client import fetch_league_scoreboard
from database import (
    get_conn,
    get_latest_prediction,
    has_auto_pushed,
    init_db,
    insert_odds_snapshot,
    list_matches_with_predictions,
    list_reviews,
    mark_auto_pushed,
    mark_feishu_pushed,
    upsert_match,
)
from engine.predictor import ENGINE_CACHE, predict_match
from engine.review_engine import review_finished_matches, summarize_review_performance
from notify.feishu_bot import (
    build_daily_brief_card,
    build_review_report_card,
    build_single_final_card,
    send_feishu_card,
)

BJT = timezone(timedelta(hours=8))


def sync_quick(days_back: int = 2, days_ahead: int = 3) -> Dict[str, int]:
    settings = load_settings()
    enabled = settings.get("enabled_leagues") or list(LEAGUES.keys())
    total, snaps = 0, 0
    for code in enabled:
        matches = fetch_league_scoreboard(code, days_back=days_back, days_ahead=days_ahead)
        for m in matches:
            upsert_match(m)
            total += 1
            odds = m.get("odds") or {}
            if odds.get("open_home_odds") and odds.get("open_draw_odds") and odds.get("open_away_odds"):
                with get_conn() as conn:
                    cnt = conn.execute(
                        "SELECT COUNT(*) FROM odds_snapshots WHERE match_id=?", (m["match_id"],)
                    ).fetchone()[0]
                if cnt == 0:
                    if insert_odds_snapshot(
                        match_id=m["match_id"],
                        source=f"{odds.get('source', 'ESPN')}(初盘)",
                        home_odds=odds["open_home_odds"],
                        draw_odds=odds["open_draw_odds"],
                        away_odds=odds["open_away_odds"],
                        asian_handicap=odds.get("asian_handicap"),
                        over_under_line=odds.get("over_under_line"),
                    ):
                        snaps += 1
            if odds.get("home_odds") and odds.get("draw_odds") and odds.get("away_odds"):
                if insert_odds_snapshot(
                    match_id=m["match_id"],
                    source=odds.get("source", "ESPN"),
                    home_odds=odds["home_odds"],
                    draw_odds=odds["draw_odds"],
                    away_odds=odds["away_odds"],
                    asian_handicap=odds.get("asian_handicap"),
                    over_under_line=odds.get("over_under_line"),
                ):
                    snaps += 1
    return {"synced_matches": total, "new_odds_snapshots": snaps}


def run_daily_brief(slot_tag: str, label: str, force: bool = False) -> Dict[str, Any]:
    now_bjt = datetime.now(BJT)
    push_key = f"cloud_brief_{now_bjt.strftime('%Y-%m-%d')}_{slot_tag}"
    if not force and has_auto_pushed(push_key):
        return {"ok": True, "skipped": True, "reason": f"今日 {label} 已推送过"}

    sync_res = sync_quick(days_back=1, days_ahead=2)
    now_utc = datetime.now(timezone.utc)
    rows = list_matches_with_predictions(status="SCHEDULED", limit=50)

    # 筛选未来 28 小时内开球的五大联赛/欧冠比赛
    target_rows = []
    for r in rows:
        try:
            kdt = datetime.fromisoformat(r["kickoff_utc"].replace("Z", "+00:00"))
        except Exception:
            continue
        hours_left = (kdt - now_utc).total_seconds() / 3600.0
        if 0.5 <= hours_left <= 28.0:
            target_rows.append(r)

    if not target_rows:
        mark_auto_pushed(push_key, "DAILY_BRIEF", "未来28小时无赛程")
        return {"ok": True, "count": 0, "sync": sync_res, "note": "未来28小时无五大联赛或欧冠赛程"}

    ENGINE_CACHE.ensure_loaded()
    bundle: List[Dict[str, Any]] = []
    for r in target_rows:
        existing = get_latest_prediction(r["match_id"])
        manual_intel = []
        if existing:
            prev_intel = json.loads(existing.get("intel_items_json") or "[]")
            manual_intel = [
                x for x in prev_intel
                if x.get("source_name") not in ("自动赛程体能计算器", "Open-Meteo气象 + 历史裁判档案")
            ]
        pred = predict_match(
            r,
            ai_intel_items=manual_intel if manual_intel else None,
            stage=existing.get("stage", "PREMATCH") if existing else "PREMATCH",
            lock=bool(existing.get("is_locked")) if existing else False,
            fetch_live_summary=True,
        )
        bundle.append({"match": r, "prediction": pred})

    card = build_daily_brief_card(
        bundle,
        title_suffix=f"{now_bjt.strftime('%m月%d日')} {label}（共{len(bundle)}场）",
    )
    res = send_feishu_card(card)
    if res.get("ok"):
        for item in bundle:
            mark_feishu_pushed(item["prediction"]["id"])
        # 仅正式定时任务写入正式打卡键；手动 force 测试绝不占用正式打卡名额
        if not force:
            mark_auto_pushed(push_key, "DAILY_BRIEF", f"{label} 推送 {len(bundle)} 场")
    return {"ok": res.get("ok"), "count": len(bundle), "feishu": res, "sync": sync_res}


def run_prematch_1h(force: bool = False) -> Dict[str, Any]:
    """检查未来 15 ~ 115 分钟内开打的比赛，容错 GitHub Actions 队列延迟，精准触发赛前约 1 小时临场更新与锁盘"""
    sync_res = sync_quick(days_back=0, days_ahead=1)
    now_utc = datetime.now(timezone.utc)
    rows = list_matches_with_predictions(status="SCHEDULED", limit=50)

    due = []
    for r in rows:
        push_key = f"prematch_1h_{r['match_id']}"
        if not force and has_auto_pushed(push_key):
            continue
        try:
            kdt = datetime.fromisoformat(r["kickoff_utc"].replace("Z", "+00:00"))
        except Exception:
            continue
        mins_left = (kdt - now_utc).total_seconds() / 60.0
        # 容错 GitHub 免费队列延迟，放宽到 15~115 分钟（靠 push_key 保证每场严格只推 1 次）
        if 15.0 <= mins_left <= 115.0:
            due.append(r)

    if not due:
        return {"ok": True, "count": 0, "note": "当前无处于赛前1小时窗口的比赛，极速退出节省云端额度", "sync": sync_res}

    ENGINE_CACHE.ensure_loaded()
    bundle: List[Dict[str, Any]] = []
    for r in due:
        existing = get_latest_prediction(r["match_id"])
        manual_intel = []
        if existing:
            prev_intel = json.loads(existing.get("intel_items_json") or "[]")
            manual_intel = [
                x for x in prev_intel
                if x.get("source_name") not in ("自动赛程体能计算器", "Open-Meteo气象 + 历史裁判档案")
            ]
        pred = predict_match(
            r,
            ai_intel_items=manual_intel,
            stage="FINAL",
            lock=True,
            fetch_live_summary=True,
        )
        bundle.append({"match": r, "prediction": pred})

    if len(bundle) == 1:
        card = build_single_final_card(bundle[0]["match"], bundle[0]["prediction"])
    else:
        card = build_daily_brief_card(
            bundle,
            title_suffix=f"⚡ 赛前1小时临场终审更新（已锁盘·{len(bundle)}场）",
        )
    res = send_feishu_card(card)
    if res.get("ok"):
        for item in bundle:
            mark_feishu_pushed(item["prediction"]["id"])
            mark_auto_pushed(
                f"prematch_1h_{item['match']['match_id']}",
                "PREMATCH_1H",
                f"{item['match']['home_team_cn']} vs {item['match']['away_team_cn']}",
            )
    return {"ok": res.get("ok"), "count": len(bundle), "feishu": res, "sync": sync_res}


def run_daily_review(force: bool = False) -> Dict[str, Any]:
    now_bjt = datetime.now(BJT)
    push_key = f"cloud_review_{now_bjt.strftime('%Y-%m-%d')}"
    if not force and has_auto_pushed(push_key):
        return {"ok": True, "skipped": True, "reason": "今日赛后复盘已推送"}

    sync_res = sync_quick(days_back=3, days_ahead=1)
    newly = review_finished_matches()
    if not newly:
        mark_auto_pushed(push_key, "DAILY_REVIEW", "昨日无已预测完场比赛")
        return {"ok": True, "newly_reviewed": 0, "sync": sync_res}

    all_reviews = list_reviews(limit=20)
    summary = summarize_review_performance(all_reviews)
    res = send_feishu_card(build_review_report_card(summary, newly))
    if res.get("ok"):
        mark_auto_pushed(push_key, "DAILY_REVIEW", f"复盘 {len(newly)} 场")
    return {"ok": res.get("ok"), "newly_reviewed": len(newly), "summary": summary, "feishu": res}


def run_auto() -> Dict[str, Any]:
    """根据北京时间自动分发任务（放宽容错窗口，就算 GitHub 免费队列延迟 1~2 小时也绝不漏发！）"""
    now_bjt = datetime.now(BJT)
    h = now_bjt.hour
    results: Dict[str, Any] = {"bjt_time": now_bjt.strftime("%Y-%m-%d %H:%M")}

    if 8 <= h <= 11:
        results["daily_review"] = run_daily_review()
    if 11 <= h <= 14:
        results["daily_noon"] = run_daily_brief("1200", "12:00 午间初判简报")
    if 19 <= h <= 22:
        results["daily_evening"] = run_daily_brief("2000", "20:00 晚间盘口更新简报")

    # 任何唤醒时段都顺带检查一次赛前约 1 小时窗口，并确保未来 48 小时赛程在手机网页上均已出预测
    results["prematch_1h"] = run_prematch_1h()
    ensure_upcoming_predicted_for_web()
    return results


def ensure_upcoming_predicted_for_web(limit: int = 20) -> int:
    """确保未来 48 小时内的所有未开赛五大联赛/欧冠在手机云端网页里都有最新预测，不等定时通知也能看"""
    now_utc = datetime.now(timezone.utc)
    rows = list_matches_with_predictions(status="SCHEDULED", limit=limit)
    missing = []
    for r in rows:
        if r.get("prediction_id"):
            continue
        try:
            kdt = datetime.fromisoformat(r["kickoff_utc"].replace("Z", "+00:00"))
            if 0.0 <= (kdt - now_utc).total_seconds() / 3600.0 <= 60.0:
                missing.append(r)
        except Exception:
            continue
    if not missing:
        return 0
    ENGINE_CACHE.ensure_loaded()
    for r in missing:
        predict_match(r, ai_intel_items=None, stage="PREMATCH", lock=False, fetch_live_summary=True)
    return len(missing)


def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub Actions 0成本云端定时推送入口")
    parser.add_argument(
        "--task",
        default="auto",
        choices=["auto", "daily_noon", "daily_evening", "prematch_1h", "daily_review"],
    )
    parser.add_argument("--force", action="store_true", help="忽略当日去重强制推送一次（测试用）")
    args = parser.parse_args()

    init_db()
    if args.task == "daily_noon":
        out = run_daily_brief("1200", "12:00 午间初判简报", force=args.force)
    elif args.task == "daily_evening":
        out = run_daily_brief("2000", "20:00 晚间盘口更新简报", force=args.force)
    elif args.task == "prematch_1h":
        out = run_prematch_1h(force=args.force)
    elif args.task == "daily_review":
        out = run_daily_review(force=args.force)
    else:
        out = run_auto()

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
