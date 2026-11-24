"""
内置自动定时推送守护调度器（零外部依赖，随本地服务自动运行）
负责 4 项自动化闭环：
1. 每 2 小时自动同步五大联赛+欧冠赛程并抓拍盘口快照（自建盘口走势曲线）
2. 每天固定时间（默认 17:00 北京时间）自动分析未来 24 小时内焦点赛程并推飞书《每日初判简报》
3. 每 5 分钟巡检一次，发现距离开球 45~75 分钟的比赛，自动拉取最新盘口+官方首发，
   保留此前已录入的 AI 情报加减分，执行临场终审并锁单，推送《⚡ 赛前1小时临场更新》到飞书
4. 每天固定时间（默认 09:30 北京时间）自动核对完场比分并推飞书《每日赛后复盘报告》
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from config import LEAGUES, load_settings
from data_sources.espn_client import fetch_league_scoreboard
from database import (
    get_conn,
    get_latest_prediction,
    has_auto_pushed,
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


class AutoPushScheduler:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_sync_at: str = ""
        self.last_check_at: str = ""
        self.last_action_log: str = "守护线程待命"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="FootballAutoScheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def status(self) -> Dict[str, Any]:
        settings = load_settings()
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "auto_push_enabled": bool(settings.get("auto_push_enabled", True)),
            "daily_push_times": settings.get("daily_push_times", ["12:00", "20:00"]),
            "review_push_time": settings.get("review_push_time", "09:30"),
            "prematch_1h_enabled": bool(settings.get("prematch_1h_enabled", True)),
            "last_sync_at": self.last_sync_at,
            "last_check_at": self.last_check_at,
            "last_action_log": self.last_action_log,
        }

    def _sync_quick(self, days_back: int = 2, days_ahead: int = 3) -> int:
        settings = load_settings()
        enabled = settings.get("enabled_leagues") or list(LEAGUES.keys())
        snaps = 0
        for code in enabled:
            matches = fetch_league_scoreboard(code, days_back=days_back, days_ahead=days_ahead)
            for m in matches:
                upsert_match(m)
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
        self.last_sync_at = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
        return snaps

    def _parse_hhmm(self, hhmm: str, default_h: int, default_m: int) -> tuple[int, int]:
        try:
            parts = hhmm.strip().split(":")
            return int(parts[0]), int(parts[1])
        except Exception:
            return default_h, default_m

    def _check_daily_brief(self, now_bjt: datetime, settings: Dict[str, Any]) -> None:
        push_times = settings.get("daily_push_times") or ["12:00", "20:00"]
        if isinstance(push_times, str):
            push_times = [x.strip() for x in push_times.split(",") if x.strip()]

        now_mins = now_bjt.hour * 60 + now_bjt.minute
        for slot in push_times:
            th, tm = self._parse_hhmm(slot, 12, 0)
            target_mins = th * 60 + tm
            # 仅在到达设定时间后的 90 分钟窗口内触发（防止晚上启动软件把中午的旧推送补发一遍）
            if not (0 <= now_mins - target_mins <= 90):
                continue
            slot_key = f"daily_brief_{now_bjt.strftime('%Y-%m-%d')}_{th:02d}{tm:02d}"
            if has_auto_pushed(slot_key):
                continue

            # 同步并筛选未来 28 小时内开打的焦点比赛（覆盖今晚+次日凌晨欧冠/五大联赛）
            self._sync_quick(days_back=1, days_ahead=2)
            ENGINE_CACHE.ensure_loaded()
            now_utc = datetime.now(timezone.utc)
            rows = list_matches_with_predictions(status="SCHEDULED", limit=50)

            bundle: List[Dict[str, Any]] = []
            for r in rows:
                try:
                    kdt = datetime.fromisoformat(r["kickoff_utc"].replace("Z", "+00:00"))
                except Exception:
                    continue
                hours_left = (kdt - now_utc).total_seconds() / 3600.0
                if 0.5 <= hours_left <= 28.0:
                    existing = get_latest_prediction(r["match_id"])
                    if existing:
                        prev_intel = json.loads(existing.get("intel_items_json") or "[]")
                        manual_intel = [
                            x for x in prev_intel if x.get("source_name") not in ("自动赛程体能计算器", "Open-Meteo气象 + 历史裁判档案")
                        ]
                        pred = predict_match(
                            r,
                            ai_intel_items=manual_intel,
                            stage=existing.get("stage", "PREMATCH"),
                            lock=bool(existing.get("is_locked")),
                            fetch_live_summary=False,
                        )
                    else:
                        pred = predict_match(r, ai_intel_items=[], stage="PREMATCH", lock=False, fetch_live_summary=False)
                    bundle.append({"match": r, "prediction": pred})

            slot_label = "午间初判简报" if th < 15 else "晚间盘口更新简报"
            if bundle:
                card = build_daily_brief_card(
                    bundle,
                    title_suffix=f"{now_bjt.strftime('%m月%d日')} {th:02d}:{tm:02d} {slot_label}（共{len(bundle)}场）",
                )
                res = send_feishu_card(card)
                if res.get("ok"):
                    for item in bundle:
                        mark_feishu_pushed(item["prediction"]["id"])
                    mark_auto_pushed(slot_key, "DAILY_BRIEF", f"{slot} 推送 {len(bundle)} 场")
                    self.last_action_log = f"{now_bjt.strftime('%H:%M')} 已自动推送{slot_label}（{len(bundle)} 场）"
            else:
                mark_auto_pushed(slot_key, "DAILY_BRIEF", f"{slot} 无赛程")
                self.last_action_log = f"{now_bjt.strftime('%H:%M')} 未来28小时无五大联赛/欧冠赛程"

    def _check_prematch_1h_updates(self, now_bjt: datetime, settings: Dict[str, Any]) -> None:
        if not settings.get("prematch_1h_enabled", True):
            return

        now_utc = datetime.now(timezone.utc)
        rows = list_matches_with_predictions(status="SCHEDULED", limit=40)
        due_matches = []
        for r in rows:
            push_key = f"prematch_1h_{r['match_id']}"
            if has_auto_pushed(push_key):
                continue
            try:
                kdt = datetime.fromisoformat(r["kickoff_utc"].replace("Z", "+00:00"))
            except Exception:
                continue
            mins_left = (kdt - now_utc).total_seconds() / 60.0
            # 开赛前 35 ~ 70 分钟窗口触发临场更新（官宣首发通常在赛前60分钟放出）
            if 35.0 <= mins_left <= 70.0:
                due_matches.append(r)

        if not due_matches:
            return

        # 发现临场窗口比赛，立刻拉一次最新赔率快照
        self._sync_quick(days_back=0, days_ahead=1)
        ENGINE_CACHE.ensure_loaded()

        updated_bundle = []
        for r in due_matches:
            existing = get_latest_prediction(r["match_id"])
            prev_intel = json.loads(existing.get("intel_items_json") or "[]") if existing else []
            # 过滤掉旧的自动触发项，让引擎用最新天气/裁判/首发重新计算，同时完整保留人工/AI写入的伤停/战意项
            manual_intel = [
                x for x in prev_intel if x.get("source_name") not in ("自动赛程体能计算器", "Open-Meteo气象 + 历史裁判档案")
            ]
            pred = predict_match(
                match=r,
                ai_intel_items=manual_intel,
                stage="FINAL",
                lock=True,
                fetch_live_summary=True,
            )
            updated_bundle.append({"match": r, "prediction": pred})

        # 如果同一时间段有多场同时开球（如凌晨04:00多场欧冠），合并发一张清爽汇总卡片，避免手机连续狂响；单场则发超详细单场卡片
        if len(updated_bundle) == 1:
            m = updated_bundle[0]["match"]
            p = updated_bundle[0]["prediction"]
            res = send_feishu_card(build_single_final_card(m, p))
        else:
            res = send_feishu_card(
                build_daily_brief_card(
                    updated_bundle,
                    title_suffix=f"⚡ 赛前1小时临场终审更新（已锁盘·{len(updated_bundle)}场）",
                )
            )

        if res.get("ok"):
            for item in updated_bundle:
                mark_feishu_pushed(item["prediction"]["id"])
                mark_auto_pushed(
                    f"prematch_1h_{item['match']['match_id']}",
                    "PREMATCH_1H",
                    f"{item['match']['home_team_cn']} vs {item['match']['away_team_cn']}",
                )
            self.last_action_log = f"{now_bjt.strftime('%H:%M')} 已自动推送 {len(updated_bundle)} 场赛前1小时临场更新"

    def _check_daily_review(self, now_bjt: datetime, settings: Dict[str, Any]) -> None:
        th, tm = self._parse_hhmm(settings.get("review_push_time", "09:30"), 9, 30)
        if (now_bjt.hour, now_bjt.minute) < (th, tm):
            return
        day_key = f"daily_review_{now_bjt.strftime('%Y-%m-%d')}"
        if has_auto_pushed(day_key):
            return

        self._sync_quick(days_back=3, days_ahead=1)
        newly = review_finished_matches()
        if newly:
            all_reviews = list_reviews(limit=20)
            summary = summarize_review_performance(all_reviews)
            res = send_feishu_card(build_review_report_card(summary, newly))
            if res.get("ok"):
                mark_auto_pushed(day_key, "DAILY_REVIEW", f"复盘 {len(newly)} 场")
                self.last_action_log = f"{now_bjt.strftime('%H:%M')} 已自动推送每日赛后复盘（{len(newly)} 场）"
        else:
            mark_auto_pushed(day_key, "DAILY_REVIEW", "昨日无新完场已预测比赛")

    def _loop(self) -> None:
        last_odds_snap_ts = 0.0
        # 启动后延迟 8 秒开始首轮巡检，避免拖慢网页启动
        if self._stop_event.wait(8.0):
            return

        while not self._stop_event.is_set():
            try:
                settings = load_settings()
                now_bjt = datetime.now(BJT)
                self.last_check_at = now_bjt.strftime("%Y-%m-%d %H:%M:%S")

                # 每 2 小时自动抓拍一次盘口走势快照
                if time.time() - last_odds_snap_ts >= 7200:
                    self._sync_quick(days_back=2, days_ahead=4)
                    last_odds_snap_ts = time.time()

                if settings.get("auto_push_enabled", True) and settings.get("feishu_webhook"):
                    self._check_daily_review(now_bjt, settings)
                    self._check_daily_brief(now_bjt, settings)
                    self._check_prematch_1h_updates(now_bjt, settings)
            except Exception as e:
                self.last_action_log = f"守护巡检异常：{e}"

            # 每 5 分钟巡检一次
            if self._stop_event.wait(300.0):
                break


SCHEDULER = AutoPushScheduler()
