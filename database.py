"""
SQLite 数据库层：负责赛程、盘口走势快照、预测锁定单、AI情报修正明细、
赛后核对复盘与经验调优库的持久化存储。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import DB_PATH


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    """初始化所有业务表"""
    with get_conn() as conn:
        conn.executescript(
            """
            -- 1. 赛程与基础信息表
            CREATE TABLE IF NOT EXISTS matches (
                match_id TEXT PRIMARY KEY,
                league_code TEXT NOT NULL,
                league_name TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                home_team_cn TEXT,
                away_team_cn TEXT,
                kickoff_utc TEXT NOT NULL,
                kickoff_bjt TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'SCHEDULED', -- SCHEDULED / LIVE / FINISHED
                home_score INTEGER,
                away_score INTEGER,
                venue TEXT,
                referee TEXT,
                espn_event_id TEXT,
                raw_json TEXT,
                updated_at TEXT NOT NULL
            );

            -- 2. 盘口走势定时快照表（0成本自建初盘 vs 临场盘走势曲线）
            CREATE TABLE IF NOT EXISTS odds_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                source TEXT NOT NULL,
                home_odds REAL,
                draw_odds REAL,
                away_odds REAL,
                asian_handicap TEXT,
                over_under_line REAL,
                home_implied REAL,
                draw_implied REAL,
                away_implied REAL,
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            );
            CREATE INDEX IF NOT EXISTS idx_odds_match_time ON odds_snapshots(match_id, captured_at);

            -- 3. 预测与实盘锁定台账（支持初判 / 临场终审多阶段，锁定后不可篡改）
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT 'PREMATCH', -- PREMATCH(初判) / FINAL(临场终审)
                is_locked INTEGER NOT NULL DEFAULT 0,   -- 1=赛前锁死实盘记录
                model_version TEXT NOT NULL,
                created_at TEXT NOT NULL,

                -- 第一层：数理基准
                base_home_xg REAL NOT NULL,
                base_away_xg REAL NOT NULL,
                base_prob_home REAL NOT NULL,
                base_prob_draw REAL NOT NULL,
                base_prob_away REAL NOT NULL,

                -- 第二层：动态情报修正明细（JSON数组，每项含维度、主/客修正%、新闻依据、出处链接）
                intel_items_json TEXT NOT NULL DEFAULT '[]',
                home_intel_delta REAL NOT NULL DEFAULT 0.0,
                away_intel_delta REAL NOT NULL DEFAULT 0.0,

                -- 天气与裁判触发状态
                weather_json TEXT NOT NULL DEFAULT '{}',
                referee_json TEXT NOT NULL DEFAULT '{}',
                fatigue_json TEXT NOT NULL DEFAULT '{}',

                -- 最终预测结果
                final_home_xg REAL NOT NULL,
                final_away_xg REAL NOT NULL,
                final_prob_home REAL NOT NULL,
                final_prob_draw REAL NOT NULL,
                final_prob_away REAL NOT NULL,
                recommended_outcome TEXT NOT NULL,      -- H / D / A
                top3_scores_json TEXT NOT NULL,         -- [{"score":"2-1","prob":0.145}, ...]
                over_25_prob REAL,
                btts_prob REAL,

                -- 第三层：市场赔率红绿灯体检
                market_prob_home REAL,
                market_prob_draw REAL,
                market_prob_away REAL,
                traffic_light TEXT NOT NULL DEFAULT 'GREEN', -- GREEN / YELLOW / RED
                traffic_note TEXT,
                odds_trend_note TEXT,

                -- 报告与自媒体草稿
                summary_text TEXT,
                media_article TEXT,
                pushed_feishu INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            );
            CREATE INDEX IF NOT EXISTS idx_pred_match ON predictions(match_id, created_at);

            -- 4. 每日赛后核对复盘与归因表（用户追加核心功能）
            CREATE TABLE IF NOT EXISTS post_match_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id TEXT NOT NULL UNIQUE,
                prediction_id INTEGER NOT NULL,
                reviewed_at TEXT NOT NULL,
                actual_home_score INTEGER NOT NULL,
                actual_away_score INTEGER NOT NULL,
                actual_outcome TEXT NOT NULL,           -- H / D / A

                outcome_hit INTEGER NOT NULL,           -- 1=胜平负主推命中
                top3_score_hit INTEGER NOT NULL,        -- 1=Top 3比分命中
                exact_top1_hit INTEGER NOT NULL,        -- 1=首选比分精准命中
                brier_score REAL NOT NULL,              -- 概率校准误差（越小越好）
                upset_alert_useful INTEGER DEFAULT 0,   -- 1=冷门/红黄灯避坑有效

                -- 偏差归因分类：ACCURATE(准确命中) / MODEL_OR_INTEL_MISS(模型或情报判断失误) / VARIANCE_LUCK(红牌/门柱等偶然运气)
                attribution_type TEXT NOT NULL,
                actual_xg_home REAL,
                actual_xg_away REAL,
                red_cards_note TEXT,
                root_cause_analysis TEXT NOT NULL,      -- 正确/错误原因深度复盘
                tuning_suggestion TEXT,                 -- 针对权重/规则的具体改进建议
                FOREIGN KEY (match_id) REFERENCES matches(match_id),
                FOREIGN KEY (prediction_id) REFERENCES predictions(id)
            );

            -- 5. 参数演进与经验调优库
            CREATE TABLE IF NOT EXISTS tuning_lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                category TEXT NOT NULL,                 -- 伤停/多线/战意/盘口/裁判/天气/数理底座
                lesson TEXT NOT NULL,
                suggested_action TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN'     -- OPEN / APPLIED / IGNORED
            );

            -- 6. 历史回测结果存档表
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                league_scope TEXT NOT NULL,
                date_from TEXT NOT NULL,
                date_to TEXT NOT NULL,
                total_matches INTEGER NOT NULL,
                outcome_accuracy REAL NOT NULL,
                top3_score_accuracy REAL NOT NULL,
                brier_score REAL NOT NULL,
                log_loss REAL NOT NULL,
                market_accuracy REAL,
                details_json TEXT NOT NULL
            );

            -- 7. 自动推送去重记录表（防止重启软件后重复刷屏飞书）
            CREATE TABLE IF NOT EXISTS auto_push_logs (
                push_key TEXT PRIMARY KEY,
                push_type TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                detail TEXT
            );
            """
        )
        # 安全热迁移 v1.5 新增列（兼容已有旧数据库零报错）
        _ensure_columns(
            conn,
            "predictions",
            {
                "lottery_json": "TEXT NOT NULL DEFAULT '{}'",
                "value_ev_json": "TEXT NOT NULL DEFAULT '{}'",
                "news_injuries_json": "TEXT NOT NULL DEFAULT '{}'",
                "confidence_score": "REAL NOT NULL DEFAULT 50.0",
                "confidence_tier": "TEXT NOT NULL DEFAULT ''",
            },
        )
        _ensure_columns(
            conn,
            "odds_snapshots",
            {
                "over_odds": "REAL",
                "under_odds": "REAL",
            },
        )
        _ensure_columns(
            conn,
            "post_match_reviews",
            {
                "confidence_score": "REAL DEFAULT 50.0",
                "clv_diff": "REAL",
            },
        )


def _ensure_columns(conn: sqlite3.Connection, table: str, cols: Dict[str, str]) -> None:
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col_name, col_def in cols.items():
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")


def has_auto_pushed(push_key: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM auto_push_logs WHERE push_key = ?", (push_key,)).fetchone()
        return row is not None


def mark_auto_pushed(push_key: str, push_type: str, detail: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO auto_push_logs (push_key, push_type, sent_at, detail)
            VALUES (?, ?, ?, ?)
            """,
            (push_key, push_type, now_iso(), detail),
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_match(match: Dict[str, Any]) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO matches (
                match_id, league_code, league_name, home_team, away_team,
                home_team_cn, away_team_cn, kickoff_utc, kickoff_bjt,
                status, home_score, away_score, venue, referee,
                espn_event_id, raw_json, updated_at
            ) VALUES (
                :match_id, :league_code, :league_name, :home_team, :away_team,
                :home_team_cn, :away_team_cn, :kickoff_utc, :kickoff_bjt,
                :status, :home_score, :away_score, :venue, :referee,
                :espn_event_id, :raw_json, :updated_at
            )
            ON CONFLICT(match_id) DO UPDATE SET
                status = excluded.status,
                home_score = COALESCE(excluded.home_score, matches.home_score),
                away_score = COALESCE(excluded.away_score, matches.away_score),
                venue = COALESCE(NULLIF(excluded.venue, ''), matches.venue),
                referee = COALESCE(NULLIF(excluded.referee, ''), matches.referee),
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at
            """,
            {
                "match_id": match["match_id"],
                "league_code": match["league_code"],
                "league_name": match["league_name"],
                "home_team": match["home_team"],
                "away_team": match["away_team"],
                "home_team_cn": match.get("home_team_cn") or match["home_team"],
                "away_team_cn": match.get("away_team_cn") or match["away_team"],
                "kickoff_utc": match["kickoff_utc"],
                "kickoff_bjt": match["kickoff_bjt"],
                "status": match.get("status", "SCHEDULED"),
                "home_score": match.get("home_score"),
                "away_score": match.get("away_score"),
                "venue": match.get("venue") or "",
                "referee": match.get("referee") or "",
                "espn_event_id": match.get("espn_event_id") or "",
                "raw_json": json.dumps(match.get("raw_json") or {}, ensure_ascii=False),
                "updated_at": now_iso(),
            },
        )


def insert_odds_snapshot(
    match_id: str,
    source: str,
    home_odds: Optional[float],
    draw_odds: Optional[float],
    away_odds: Optional[float],
    asian_handicap: Optional[str] = None,
    over_under_line: Optional[float] = None,
    over_odds: Optional[float] = None,
    under_odds: Optional[float] = None,
) -> Optional[int]:
    if not (home_odds and draw_odds and away_odds):
        return None
    if home_odds <= 1.0 or draw_odds <= 1.0 or away_odds <= 1.0:
        return None
    # v1.5 升级：使用更精准的 Power 幂次去水法修正机构冷门抽水偏差
    from engine.market_math import power_devig

    h_imp, d_imp, a_imp = power_devig(home_odds, draw_odds, away_odds)

    with get_conn() as conn:
        # 避免短时间内重复存入完全相同的赔率快照
        last = conn.execute(
            """
            SELECT home_odds, draw_odds, away_odds, asian_handicap, over_odds, under_odds
            FROM odds_snapshots
            WHERE match_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (match_id,),
        ).fetchone()
        if (
            last
            and last["home_odds"] == home_odds
            and last["draw_odds"] == draw_odds
            and last["away_odds"] == away_odds
            and (last["asian_handicap"] or "") == (asian_handicap or "")
            and last["over_odds"] == over_odds
            and last["under_odds"] == under_odds
        ):
            return None

        cur = conn.execute(
            """
            INSERT INTO odds_snapshots (
                match_id, captured_at, source,
                home_odds, draw_odds, away_odds,
                asian_handicap, over_under_line, over_odds, under_odds,
                home_implied, draw_implied, away_implied
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_id,
                now_iso(),
                source,
                home_odds,
                draw_odds,
                away_odds,
                asian_handicap,
                over_under_line,
                over_odds,
                under_odds,
                h_imp,
                d_imp,
                a_imp,
            ),
        )
        return cur.lastrowid


def get_odds_history(match_id: str) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM odds_snapshots WHERE match_id = ? ORDER BY captured_at ASC",
            (match_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def save_prediction(pred: Dict[str, Any]) -> int:
    pred.setdefault("lottery_json", "{}")
    pred.setdefault("value_ev_json", "{}")
    pred.setdefault("news_injuries_json", "{}")
    pred.setdefault("confidence_score", 50.0)
    pred.setdefault("confidence_tier", "")
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO predictions (
                match_id, stage, is_locked, model_version, created_at,
                base_home_xg, base_away_xg, base_prob_home, base_prob_draw, base_prob_away,
                intel_items_json, home_intel_delta, away_intel_delta,
                weather_json, referee_json, fatigue_json,
                final_home_xg, final_away_xg, final_prob_home, final_prob_draw, final_prob_away,
                recommended_outcome, top3_scores_json, over_25_prob, btts_prob,
                market_prob_home, market_prob_draw, market_prob_away,
                traffic_light, traffic_note, odds_trend_note,
                summary_text, media_article, pushed_feishu,
                lottery_json, value_ev_json, news_injuries_json, confidence_score, confidence_tier
            ) VALUES (
                :match_id, :stage, :is_locked, :model_version, :created_at,
                :base_home_xg, :base_away_xg, :base_prob_home, :base_prob_draw, :base_prob_away,
                :intel_items_json, :home_intel_delta, :away_intel_delta,
                :weather_json, :referee_json, :fatigue_json,
                :final_home_xg, :final_away_xg, :final_prob_home, :final_prob_draw, :final_prob_away,
                :recommended_outcome, :top3_scores_json, :over_25_prob, :btts_prob,
                :market_prob_home, :market_prob_draw, :market_prob_away,
                :traffic_light, :traffic_note, :odds_trend_note,
                :summary_text, :media_article, :pushed_feishu,
                :lottery_json, :value_ev_json, :news_injuries_json, :confidence_score, :confidence_tier
            )
            """,
            pred,
        )
        return cur.lastrowid


def get_latest_prediction(match_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM predictions WHERE match_id = ? ORDER BY is_locked DESC, id DESC LIMIT 1",
            (match_id,),
        ).fetchone()
        return dict(row) if row else None


def lock_prediction(prediction_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE predictions SET is_locked = 1 WHERE id = ?", (prediction_id,))


def mark_feishu_pushed(prediction_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE predictions SET pushed_feishu = 1 WHERE id = ?", (prediction_id,))


def list_matches_with_predictions(
    league_code: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 80,
) -> List[Dict[str, Any]]:
    sql = """
        SELECT m.*,
               p.id AS prediction_id,
               p.stage AS pred_stage,
               p.is_locked AS pred_locked,
               p.final_prob_home, p.final_prob_draw, p.final_prob_away,
               p.final_home_xg, p.final_away_xg,
               p.recommended_outcome, p.top3_scores_json,
               p.traffic_light, p.traffic_note, p.odds_trend_note,
               p.home_intel_delta, p.away_intel_delta,
               p.pushed_feishu,
               r.outcome_hit, r.top3_score_hit, r.attribution_type
        FROM matches m
        LEFT JOIN (
            SELECT p1.*
            FROM predictions p1
            INNER JOIN (
                SELECT match_id, MAX(id) AS max_id
                FROM predictions
                GROUP BY match_id
            ) p2 ON p1.match_id = p2.match_id AND p1.id = p2.max_id
        ) p ON m.match_id = p.match_id
        LEFT JOIN post_match_reviews r ON m.match_id = r.match_id
        WHERE 1=1
    """
    params: List[Any] = []
    if league_code and league_code != "ALL":
        sql += " AND m.league_code = ?"
        params.append(league_code)
    if status and status != "ALL":
        if status == "STARTED":
            sql += " AND m.status IN ('LIVE', 'FINISHED')"
        else:
            sql += " AND m.status = ?"
            params.append(status)
    sql += " ORDER BY m.kickoff_utc ASC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_match_detail(match_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        m = conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
        if not m:
            return None
        match_dict = dict(m)
        pred = get_latest_prediction(match_id)
        odds = get_odds_history(match_id)
        review = conn.execute(
            "SELECT * FROM post_match_reviews WHERE match_id = ?", (match_id,)
        ).fetchone()
        return {
            "match": match_dict,
            "prediction": pred,
            "odds_history": odds,
            "review": dict(review) if review else None,
        }


def save_review(review: Dict[str, Any]) -> None:
    review.setdefault("confidence_score", 50.0)
    review.setdefault("clv_diff", None)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO post_match_reviews (
                match_id, prediction_id, reviewed_at,
                actual_home_score, actual_away_score, actual_outcome,
                outcome_hit, top3_score_hit, exact_top1_hit, brier_score,
                upset_alert_useful, attribution_type,
                actual_xg_home, actual_xg_away, red_cards_note,
                root_cause_analysis, tuning_suggestion,
                confidence_score, clv_diff
            ) VALUES (
                :match_id, :prediction_id, :reviewed_at,
                :actual_home_score, :actual_away_score, :actual_outcome,
                :outcome_hit, :top3_score_hit, :exact_top1_hit, :brier_score,
                :upset_alert_useful, :attribution_type,
                :actual_xg_home, :actual_xg_away, :red_cards_note,
                :root_cause_analysis, :tuning_suggestion,
                :confidence_score, :clv_diff
            )
            ON CONFLICT(match_id) DO UPDATE SET
                reviewed_at = excluded.reviewed_at,
                actual_home_score = excluded.actual_home_score,
                actual_away_score = excluded.actual_away_score,
                actual_outcome = excluded.actual_outcome,
                outcome_hit = excluded.outcome_hit,
                top3_score_hit = excluded.top3_score_hit,
                exact_top1_hit = excluded.exact_top1_hit,
                brier_score = excluded.brier_score,
                upset_alert_useful = excluded.upset_alert_useful,
                attribution_type = excluded.attribution_type,
                actual_xg_home = COALESCE(excluded.actual_xg_home, post_match_reviews.actual_xg_home),
                actual_xg_away = COALESCE(excluded.actual_xg_away, post_match_reviews.actual_xg_away),
                red_cards_note = COALESCE(excluded.red_cards_note, post_match_reviews.red_cards_note),
                root_cause_analysis = excluded.root_cause_analysis,
                tuning_suggestion = excluded.tuning_suggestion,
                confidence_score = excluded.confidence_score,
                clv_diff = excluded.clv_diff
            """,
            review,
        )


def add_tuning_lesson(category: str, lesson: str, suggested_action: str) -> int:
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT id FROM tuning_lessons WHERE category = ? AND lesson = ? LIMIT 1",
            (category, lesson),
        ).fetchone()
        if exists:
            return int(exists["id"])
        cur = conn.execute(
            """
            INSERT INTO tuning_lessons (created_at, category, lesson, suggested_action)
            VALUES (?, ?, ?, ?)
            """,
            (now_iso(), category, lesson, suggested_action),
        )
        return cur.lastrowid


def list_reviews(limit: int = 100) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.*, m.league_code, m.league_name, m.home_team, m.away_team,
                   m.home_team_cn, m.away_team_cn, m.kickoff_bjt,
                   p.recommended_outcome, p.top3_scores_json, p.traffic_light,
                   p.final_prob_home, p.final_prob_draw, p.final_prob_away,
                   p.over_25_prob, p.stage, p.is_locked,
                   p.home_intel_delta, p.away_intel_delta
            FROM post_match_reviews r
            JOIN matches m ON r.match_id = m.match_id
            JOIN predictions p ON r.prediction_id = p.id
            ORDER BY substr(m.kickoff_bjt, 1, 10) DESC, r.confidence_score DESC, m.kickoff_bjt ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_tuning_lessons(limit: int = 50) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tuning_lessons ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def save_backtest_run(run: Dict[str, Any]) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO backtest_runs (
                created_at, league_scope, date_from, date_to,
                total_matches, outcome_accuracy, top3_score_accuracy,
                brier_score, log_loss, market_accuracy, details_json
            ) VALUES (
                :created_at, :league_scope, :date_from, :date_to,
                :total_matches, :outcome_accuracy, :top3_score_accuracy,
                :brier_score, :log_loss, :market_accuracy, :details_json
            )
            """,
            run,
        )
        return cur.lastrowid


def get_latest_backtest() -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM backtest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
