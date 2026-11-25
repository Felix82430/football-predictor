"""
足球比分预测与复盘系统 —— 命令行总控（供 Antigravity Skill 方案B 及定时任务直接调用）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from config import LEAGUES, load_settings
from data_sources.espn_client import fetch_league_scoreboard
from database import (
    get_latest_prediction,
    get_match_detail,
    init_db,
    insert_odds_snapshot,
    list_matches_with_predictions,
    list_reviews,
    mark_feishu_pushed,
    upsert_match,
)
from engine.backtest import run_rolling_backtest
from engine.predictor import ENGINE_CACHE, predict_match
from engine.review_engine import review_finished_matches, summarize_review_performance
from notify.feishu_bot import (
    build_daily_brief_card,
    build_review_report_card,
    build_single_final_card,
    send_feishu_card,
)


def cmd_sync(args: argparse.Namespace) -> None:
    init_db()
    settings = load_settings()
    enabled = settings.get("enabled_leagues") or list(LEAGUES.keys())
    total, snaps = 0, 0
    per_league = {}
    for code in enabled:
        matches = fetch_league_scoreboard(code, days_back=args.days_back, days_ahead=args.days_ahead)
        per_league[code] = len(matches)
        for m in matches:
            upsert_match(m)
            total += 1
            odds = m.get("odds") or {}
            if odds.get("open_home_odds") and odds.get("open_draw_odds") and odds.get("open_away_odds"):
                from database import get_conn
                with get_conn() as conn:
                    cnt = conn.execute("SELECT COUNT(*) FROM odds_snapshots WHERE match_id=?", (m["match_id"],)).fetchone()[0]
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
                    over_odds=odds.get("over_odds"),
                    under_odds=odds.get("under_odds"),
                ):
                    snaps += 1
    print(json.dumps({"ok": True, "synced_matches": total, "new_odds_snapshots": snaps, "per_league": per_league}, ensure_ascii=False, indent=2))


def cmd_list(args: argparse.Namespace) -> None:
    init_db()
    rows = list_matches_with_predictions(league_code=args.league, status=args.status, limit=args.limit)
    out = []
    for r in rows:
        out.append(
            {
                "match_id": r["match_id"],
                "league": r["league_name"],
                "kickoff_bjt": r["kickoff_bjt"],
                "match": f"{r['home_team_cn']} vs {r['away_team_cn']}",
                "status": r["status"],
                "score": f"{r['home_score']}-{r['away_score']}" if r["home_score"] is not None else "-",
                "recommended": r.get("recommended_outcome"),
                "traffic_light": r.get("traffic_light"),
                "locked": bool(r.get("pred_locked")),
            }
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_analyze(args: argparse.Namespace) -> None:
    init_db()
    ENGINE_CACHE.ensure_loaded()
    intel_items = []
    if args.intel_file:
        intel_items = json.loads(Path(args.intel_file).read_text(encoding="utf-8"))

    if args.match_id:
        detail = get_match_detail(args.match_id)
        if not detail:
            print(json.dumps({"ok": False, "error": f"未找到比赛 {args.match_id}"}, ensure_ascii=False))
            sys.exit(1)
        pred = predict_match(
            match=detail["match"],
            ai_intel_items=intel_items,
            stage=args.stage,
            lock=args.lock,
            fetch_live_summary=True,
        )
        print(json.dumps({"ok": True, "match": f"{detail['match']['home_team_cn']} vs {detail['match']['away_team_cn']}", "summary": pred["summary_text"], "traffic_light": pred["traffic_light"], "traffic_note": pred["traffic_note"], "prediction_id": pred["id"]}, ensure_ascii=False, indent=2))
    else:
        rows = list_matches_with_predictions(status="SCHEDULED", limit=args.limit)
        results = []
        for r in rows:
            pred = predict_match(match=r, ai_intel_items=None, stage=args.stage, lock=args.lock, fetch_live_summary=True)
            results.append({"match_id": r["match_id"], "match": f"{r['home_team_cn']} vs {r['away_team_cn']}", "league": r["league_name"], "kickoff_bjt": r["kickoff_bjt"], "summary": pred["summary_text"], "traffic_light": pred["traffic_light"], "confidence": pred["confidence_score"]})
        print(json.dumps({"ok": True, "count": len(results), "results": results}, ensure_ascii=False, indent=2))


def cmd_backtest(args: argparse.Namespace) -> None:
    init_db()
    res = run_rolling_backtest(lookback_days=args.days)
    out = {k: v for k, v in res.items() if k != "details_json"}
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_review(args: argparse.Namespace) -> None:
    init_db()
    custom_notes = {}
    if args.notes_file:
        custom_notes = json.loads(Path(args.notes_file).read_text(encoding="utf-8"))
    reviewed = review_finished_matches(custom_notes=custom_notes)
    all_reviews = list_reviews(limit=100)
    summary = summarize_review_performance(all_reviews)
    print(json.dumps({"ok": True, "newly_reviewed": len(reviewed), "summary": summary, "recent": reviewed[:15]}, ensure_ascii=False, indent=2))


def cmd_push_feishu(args: argparse.Namespace) -> None:
    init_db()
    if args.mode == "daily":
        rows = list_matches_with_predictions(status="SCHEDULED", limit=10)
        bundle = []
        for r in rows:
            pred = get_latest_prediction(r["match_id"])
            if pred:
                bundle.append({"match": r, "prediction": pred})
        if not bundle:
            print(json.dumps({"ok": False, "error": "无可推送场次"}, ensure_ascii=False))
            return
        res = send_feishu_card(build_daily_brief_card(bundle))
        if res.get("ok"):
            for item in bundle:
                mark_feishu_pushed(item["prediction"]["id"])
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.mode == "match":
        detail = get_match_detail(args.match_id)
        if not detail or not detail.get("prediction"):
            print(json.dumps({"ok": False, "error": "该场尚未分析"}, ensure_ascii=False))
            return
        res = send_feishu_card(build_single_final_card(detail["match"], detail["prediction"]))
        if res.get("ok"):
            mark_feishu_pushed(detail["prediction"]["id"])
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.mode == "review":
        reviews = list_reviews(limit=20)
        summary = summarize_review_performance(reviews)
        res = send_feishu_card(build_review_report_card(summary, reviews))
        print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    init_db()
    uvicorn.run("web.app:app", host="127.0.0.1", port=args.port, reload=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="足球比分量化情报与复盘系统 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="同步五大联赛与欧冠赛程、比分及盘口快照")
    p_sync.add_argument("--days-back", type=int, default=3)
    p_sync.add_argument("--days-ahead", type=int, default=7)
    p_sync.set_defaults(func=cmd_sync)

    p_list = sub.add_parser("list", help="列出数据库中的赛程与预测摘要")
    p_list.add_argument("--league", default="ALL")
    p_list.add_argument("--status", default="SCHEDULED")
    p_list.add_argument("--limit", type=int, default=30)
    p_list.set_defaults(func=cmd_list)

    p_an = sub.add_parser("analyze", help="执行单场或批量预测分析")
    p_an.add_argument("--match-id", default=None)
    p_an.add_argument("--intel-file", default=None, help="AI 情报修正 JSON 文件路径")
    p_an.add_argument("--stage", default="PREMATCH", choices=["PREMATCH", "FINAL"])
    p_an.add_argument("--lock", action="store_true", help="是否锁死为赛前实盘记录")
    p_an.add_argument("--limit", type=int, default=20)
    p_an.set_defaults(func=cmd_analyze)

    p_bt = sub.add_parser("backtest", help="执行无未来函数历史滚动回测")
    p_bt.add_argument("--days", type=int, default=45)
    p_bt.set_defaults(func=cmd_backtest)

    p_rev = sub.add_parser("review", help="每日赛后核对完场比分与归因复盘")
    p_rev.add_argument("--notes-file", default=None, help="AI 深度复盘归因覆盖 JSON")
    p_rev.set_defaults(func=cmd_review)

    p_fs = sub.add_parser("push-feishu", help="向飞书机器人推送卡片")
    p_fs.add_argument("--mode", required=True, choices=["daily", "match", "review"])
    p_fs.add_argument("--match-id", default=None)
    p_fs.set_defaults(func=cmd_push_feishu)

    p_sv = sub.add_parser("serve", help="启动本地网页版仪表盘")
    p_sv.add_argument("--port", type=int, default=8765)
    p_sv.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
