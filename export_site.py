"""
手机直看云端静态站生成器（GitHub Pages 0成本自动发布）
将数据库中的赛程、按把握度排序的预测、体彩五大玩法、5×5比分热力图、
盘口走势快照、自媒体草稿、每日赛后复盘（含星级分层/CLV）及历史滚动回测
一键打包为 docs/data.json + 手机竖屏深度适配的 docs/index.html。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from config import BASE_DIR
from database import (
    get_conn,
    get_latest_backtest,
    get_latest_prediction,
    get_odds_history,
    init_db,
    list_matches_with_predictions,
    list_reviews,
    list_tuning_lessons,
)
from engine.predictor import compute_confidence_score
from engine.review_engine import summarize_review_performance

DOCS_DIR = BASE_DIR / "docs"
BJT = timezone(timedelta(hours=8))


def _parse_json_safe(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def export_static_site() -> Dict[str, Any]:
    init_db()
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    with get_conn() as conn:
        upcoming = conn.execute("SELECT COUNT(*) FROM matches WHERE status='SCHEDULED'").fetchone()[0]
        locked = conn.execute("SELECT COUNT(DISTINCT match_id) FROM predictions WHERE is_locked=1").fetchone()[0]
        snaps = conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]

    # 导出全部近期赛程（未开赛 + 近期已完场）及完整单场详情
    raw_matches = list_matches_with_predictions(league_code="ALL", status="ALL", limit=120)
    matches_out = []
    for m in raw_matches:
        mid = m["match_id"]
        pred = get_latest_prediction(mid)
        odds_hist = get_odds_history(mid)

        pred_clean = None
        conf = None
        if pred:
            conf = compute_confidence_score(pred)
            pred_clean = {
                "id": pred["id"],
                "stage": pred["stage"],
                "is_locked": bool(pred["is_locked"]),
                "created_at": pred["created_at"],
                "base_home_xg": pred["base_home_xg"],
                "base_away_xg": pred["base_away_xg"],
                "base_prob_home": pred["base_prob_home"],
                "base_prob_draw": pred["base_prob_draw"],
                "base_prob_away": pred["base_prob_away"],
                "final_home_xg": pred["final_home_xg"],
                "final_away_xg": pred["final_away_xg"],
                "final_prob_home": pred["final_prob_home"],
                "final_prob_draw": pred["final_prob_draw"],
                "final_prob_away": pred["final_prob_away"],
                "recommended_outcome": pred["recommended_outcome"],
                "top3_scores": _parse_json_safe(pred.get("top3_scores_json"), []),
                "over_25_prob": pred.get("over_25_prob"),
                "btts_prob": pred.get("btts_prob"),
                "market_prob_home": pred.get("market_prob_home"),
                "market_prob_draw": pred.get("market_prob_draw"),
                "market_prob_away": pred.get("market_prob_away"),
                "traffic_light": pred.get("traffic_light"),
                "traffic_note": pred.get("traffic_note"),
                "odds_trend_note": pred.get("odds_trend_note"),
                "home_intel_delta": pred.get("home_intel_delta", 0.0),
                "away_intel_delta": pred.get("away_intel_delta", 0.0),
                "intel_items": _parse_json_safe(pred.get("intel_items_json"), []),
                "weather": _parse_json_safe(pred.get("weather_json"), {}),
                "referee": _parse_json_safe(pred.get("referee_json"), {}),
                "fatigue": _parse_json_safe(pred.get("fatigue_json"), {}),
                "lottery": _parse_json_safe(pred.get("lottery_json"), {}),
                "value_ev": _parse_json_safe(pred.get("value_ev_json"), {}),
                "news_injuries": _parse_json_safe(pred.get("news_injuries_json"), {}),
                "media_article": pred.get("media_article") or "",
                "confidence": conf,
            }

        matches_out.append(
            {
                "match_id": mid,
                "league_code": m["league_code"],
                "league_name": m["league_name"],
                "home_team_cn": m["home_team_cn"],
                "away_team_cn": m["away_team_cn"],
                "kickoff_bjt": m["kickoff_bjt"],
                "kickoff_utc": m["kickoff_utc"],
                "status": m["status"],
                "home_score": m["home_score"],
                "away_score": m["away_score"],
                "venue": m.get("venue") or "",
                "prediction": pred_clean,
                "odds_history": [
                    {
                        "captured_at": o["captured_at"],
                        "source": o["source"],
                        "home_odds": o["home_odds"],
                        "draw_odds": o["draw_odds"],
                        "away_odds": o["away_odds"],
                        "home_implied": o["home_implied"],
                        "draw_implied": o["draw_implied"],
                        "away_implied": o["away_implied"],
                    }
                    for o in odds_hist[-15:]
                ],
            }
        )

    reviews = list_reviews(limit=100)
    for r in reviews:
        r["top3_scores"] = _parse_json_safe(r.get("top3_scores_json"), [])
    review_summary = summarize_review_performance(reviews)
    lessons = list_tuning_lessons(limit=40)

    bt = get_latest_backtest()
    if bt:
        bt["details"] = _parse_json_safe(bt.get("details_json"), {})
        bt.pop("details_json", None)

    updated_bjt = datetime.now(BJT).strftime("%Y-%m-%d %H:%M")
    payload = {
        "updated_bjt": updated_bjt,
        "overview": {
            "upcoming_matches": upcoming,
            "locked_predictions": locked,
            "odds_snapshots": snaps,
        },
        "matches": matches_out,
        "reviews": {
            "summary": review_summary,
            "items": reviews,
            "lessons": lessons,
        },
        "backtest": bt,
    }

    (DOCS_DIR / "data.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    (DOCS_DIR / "index.html").write_text(MOBILE_HTML, encoding="utf-8")
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")
    return {"ok": True, "updated_bjt": updated_bjt, "matches_exported": len(matches_out), "reviews_exported": len(reviews)}


MOBILE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>⚽ 足球量化情报官 · 五大联赛与欧冠</title>
  <style>
    :root {
      --bg: #0b1120;
      --panel: #111a2e;
      --panel2: #17233d;
      --border: #243454;
      --text: #e6edf7;
      --muted: #8ea1c0;
      --accent: #38bdf8;
      --green: #22c55e;
      --yellow: #f59e0b;
      --red: #ef4444;
    }
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    body {
      margin: 0; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
      background: var(--bg); color: var(--text); line-height: 1.5; padding-bottom: 68px;
    }
    header {
      position: sticky; top: 0; z-index: 40;
      background: rgba(11, 17, 32, 0.94); backdrop-filter: blur(10px);
      border-bottom: 1px solid var(--border); padding: 10px 18px;
    }
    .head-top { max-width: 1180px; margin: 0 auto; display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
    .brand { font-size: 17px; font-weight: 800; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .upd-tag { font-size: 11px; font-weight: 600; color: var(--accent); background: rgba(56,189,248,.12); padding: 2px 9px; border-radius: 999px; border: 1px solid rgba(56,189,248,.28); }
    .top-nav { display: none; align-items: center; gap: 6px; flex-wrap: wrap; }
    .top-tab {
      background: transparent; color: var(--muted); border: 1px solid transparent;
      padding: 7px 14px; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600; transition: .18s;
    }
    .top-tab:hover { color: var(--text); background: var(--panel); }
    .top-tab.active { color: #fff; background: #1d4ed8; border-color: #3b82f6; }
    .container { max-width: 1180px; margin: 0 auto; padding: 14px 14px 28px; }

    /* 底部手机固定导航栏（电脑宽屏自动隐藏，改用右上角导航） */
    .bottom-nav {
      position: fixed; bottom: 0; left: 0; right: 0; z-index: 60;
      background: rgba(15, 23, 42, 0.97); backdrop-filter: blur(10px);
      border-top: 1px solid var(--border); display: grid; grid-template-columns: repeat(4, 1fr);
      padding: 6px 8px calc(6px + env(safe-area-inset-bottom));
    }
    .nav-item {
      background: none; border: none; color: var(--muted); font-size: 11px; font-weight: 700;
      padding: 6px 4px; border-radius: 8px; cursor: pointer; display: flex; flex-direction: column; align-items: center; gap: 2px;
    }
    .nav-item.active { color: var(--accent); background: rgba(56,189,248,.12); }
    @media (min-width: 720px) {
      body { padding-bottom: 24px; }
      .top-nav { display: flex; }
      .bottom-nav { display: none; }
    }

    .kpi-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-bottom: 12px; }
    @media (min-width: 720px) { .kpi-grid { grid-template-columns: repeat(4, 1fr); } }
    .kpi { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; }
    .kpi-l { font-size: 11px; color: var(--muted); }
    .kpi-v { font-size: 20px; font-weight: 800; color: #fff; margin-top: 2px; }
    .kpi-s { font-size: 11px; color: var(--accent); }

    .scroll-row { display: flex; gap: 6px; overflow-x: auto; padding-bottom: 6px; margin-bottom: 8px; scrollbar-width: none; }
    .scroll-row::-webkit-scrollbar { display: none; }
    .chip {
      flex: 0 0 auto; padding: 5px 12px; border-radius: 999px; font-size: 12px; font-weight: 600;
      background: var(--panel); border: 1px solid var(--border); color: var(--muted); cursor: pointer;
    }
    .chip.active { background: var(--accent); color: #061325; border-color: var(--accent); }

    .filter-bar { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; justify-content: space-between; }
    .status-seg { display: inline-flex; gap: 6px; flex-wrap: wrap; background: var(--panel); padding: 4px; border-radius: 10px; border: 1px solid var(--border); }
    .st-btn {
      background: transparent; color: var(--muted); border: none; border-radius: 7px;
      padding: 6px 12px; font-size: 12px; font-weight: 700; cursor: pointer; transition: .15s;
    }
    .st-btn:hover { color: var(--text); }
    .st-btn.active { background: #1d4ed8; color: #fff; }
    .day-group { margin-bottom: 16px; }
    .day-head {
      display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;
      background: linear-gradient(90deg, rgba(56,189,248,.14), rgba(17,26,46,.95));
      border: 1px solid rgba(56,189,248,.30); border-radius: 10px; padding: 9px 13px; margin-bottom: 9px;
      font-size: 13px; font-weight: 700;
    }
    select {
      background: var(--panel); color: var(--text); border: 1px solid var(--border);
      border-radius: 8px; padding: 6px 10px; font-size: 12px; min-width: 150px;
    }

    .match-grid { display: grid; grid-template-columns: 1fr; gap: 10px; }
    @media (min-width: 760px) { .match-grid { grid-template-columns: repeat(2, 1fr); } }

    .card {
      background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 13px;
      cursor: pointer; transition: .15s;
    }
    .card:active { transform: scale(0.99); }
    .rank-bar {
      display: flex; justify-content: space-between; align-items: center;
      padding: 4px 8px; border-radius: 7px; margin-bottom: 8px; font-size: 12px;
      background: rgba(56,189,248,.10); border: 1px solid rgba(56,189,248,.24);
    }
    .mc-meta { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: var(--muted); margin-bottom: 8px; }
    .lg-tag { background: rgba(56,189,248,.15); color: var(--accent); padding: 1px 7px; border-radius: 5px; font-weight: 700; }
    .teams { display: flex; justify-content: space-between; align-items: center; font-size: 16px; font-weight: 800; gap: 6px; margin-bottom: 8px; }
    .vs-pill { font-size: 14px; padding: 2px 9px; border-radius: 7px; background: #1e293b; font-family: Consolas, monospace; }

    .prob-bar { height: 9px; border-radius: 999px; overflow: hidden; display: flex; background: #1e293b; margin: 6px 0; }
    .pb-h { background: #3b82f6; } .pb-d { background: #94a3b8; } .pb-a { background: #f97316; }
    .prob-txt { display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); margin-bottom: 8px; }

    .pill-row { display: flex; gap: 5px; flex-wrap: wrap; align-items: center; margin-bottom: 6px; }
    .pill { font-size: 11px; padding: 2px 7px; border-radius: 6px; background: var(--panel2); border: 1px solid var(--border); }
    .pill strong { color: var(--accent); }
    .light-GREEN { background: rgba(34,197,94,.16); color: #4ade80; padding: 2px 7px; border-radius: 6px; font-size: 11px; font-weight: 700; }
    .light-YELLOW { background: rgba(245,158,11,.16); color: #fbbf24; padding: 2px 7px; border-radius: 6px; font-size: 11px; font-weight: 700; }
    .light-RED { background: rgba(239,68,68,.18); color: #f87171; padding: 2px 7px; border-radius: 6px; font-size: 11px; font-weight: 700; }

    .section { display: none; }
    .section.active { display: block; }
    .box { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 12px 13px; margin-bottom: 12px; }
    .box h4 { margin: 0 0 8px; font-size: 14px; color: var(--accent); }

    /* 手机全屏详情弹窗 */
    .modal {
      position: fixed; inset: 0; background: rgba(2,6,23,.85); z-index: 100;
      display: none; justify-content: center; align-items: flex-end;
    }
    .modal.open { display: flex; }
    .sheet {
      width: min(760px, 100vw); height: 92vh; overflow-y: auto;
      background: #0f172a; border-top: 1px solid var(--border); border-radius: 16px 16px 0 0;
      padding: 16px 14px 40px;
    }
    .sheet-head {
      position: sticky; top: -16px; background: #0f172a; padding: 10px 0 12px; margin-bottom: 10px;
      border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; z-index: 5;
    }
    .btn {
      padding: 6px 12px; border-radius: 8px; border: 1px solid var(--border);
      background: var(--panel2); color: #fff; font-size: 12px; font-weight: 600; cursor: pointer;
    }
    .btn-green { background: #15803d; border-color: #22c55e; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { padding: 7px 6px; border-bottom: 1px solid var(--border); text-align: left; }
    th { color: var(--muted); }
    textarea { width: 100%; min-height: 180px; background: #0b1326; color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 8px; font-size: 12px; }
    .legend { font-size: 11px; color: var(--muted); background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; margin-bottom: 10px; }
  </style>
</head>
<body>
  <header>
    <div class="head-top">
      <div class="brand">
        <span>⚽ 足球量化情报官</span>
        <span class="upd-tag" id="updTime">加载中...</span>
      </div>
      <nav class="top-nav">
        <button class="top-tab active" data-tab="matches" onclick="goTab('matches')">赛程与预测</button>
        <button class="top-tab" data-tab="reviews" onclick="goTab('reviews')">每日赛后复盘</button>
        <button class="top-tab" data-tab="backtest" onclick="goTab('backtest')">历史滚动回测</button>
        <button class="top-tab" data-tab="settings" onclick="goTab('settings')">飞书推送与设置</button>
      </nav>
    </div>
  </header>

  <div class="container">
    <div class="kpi-grid">
      <div class="kpi"><div class="kpi-l">近期焦点赛程</div><div class="kpi-v" id="kUp">-</div><div class="kpi-s" id="kSnap">盘口快照 -</div></div>
      <div class="kpi"><div class="kpi-l">实盘赛后命中率</div><div class="kpi-v" id="kRevAcc">-</div><div class="kpi-s" id="kRevSub">待核对</div></div>
      <div class="kpi"><div class="kpi-l">高把握(≥68分)猜中</div><div class="kpi-v" id="kHighAcc" style="color:#4ade80;">-</div><div class="kpi-s" id="kClv">防冷双选 -</div></div>
      <div class="kpi"><div class="kpi-l">45天历史无作弊验准</div><div class="kpi-v" id="kBt">-</div><div class="kpi-s" id="kBtSub">五大联赛真实统计</div></div>
    </div>

    <!-- Tab 1: 赛程预测 -->
    <div id="tab-matches" class="section active">
      <div class="legend">🟢绿灯=模型和全球赔率看法一致(最稳优先看) &nbsp;|&nbsp; 🟡黄灯=看法有分歧(建议防平或等赛前1小时看首发) &nbsp;|&nbsp; 🔴红灯=突然变盘或分歧太大(容易爆冷慎碰)</div>
      <div class="filter-bar">
        <div class="status-seg" id="statusSeg">
          <button class="st-btn active" data-st="SCHEDULED" onclick="setStatus('SCHEDULED', this)">⏳ 未开赛 <span id="cntSched"></span></button>
          <button class="st-btn" data-st="STARTED" onclick="setStatus('STARTED', this)">🏁 已开赛/完场 <span id="cntStart"></span></button>
          <button class="st-btn" data-st="ALL" onclick="setStatus('ALL', this)">📋 全部 <span id="cntAll"></span></button>
        </div>
        <select id="sortSel" onchange="renderMatches()">
          <option value="confidence" selected>🏆 按把握度从高到低</option>
          <option value="time">🕒 按开球时间</option>
        </select>
      </div>
      <div class="scroll-row" id="lgChips">
        <span class="chip active" onclick="setLg('ALL', this)">全部</span>
        <span class="chip" onclick="setLg('UCL', this)">欧冠</span>
        <span class="chip" onclick="setLg('EPL', this)">英超</span>
        <span class="chip" onclick="setLg('LALIGA', this)">西甲</span>
        <span class="chip" onclick="setLg('SERIEA', this)">意甲</span>
        <span class="chip" onclick="setLg('BUNDESLIGA', this)">德甲</span>
        <span class="chip" onclick="setLg('LIGUE1', this)">法甲</span>
      </div>
      <div class="match-grid" id="matchGrid"></div>
    </div>

    <!-- Tab 2: 每日复盘 -->
    <div id="tab-reviews" class="section">
      <div class="box">
        <h4>📊 实盘战绩与按星级分层命中率</h4>
        <div id="revSummary" style="font-size:13px;"></div>
      </div>
      <div id="revCards"></div>
    </div>

    <!-- Tab 3: 历史回测 -->
    <div id="tab-backtest" class="section">
      <div id="btWrap"></div>
    </div>

    <!-- Tab 4: 飞书推送与设置 -->
    <div id="tab-settings" class="section">
      <div class="box">
        <h4>📲 云端自动推送计划（电脑关机照常运行）</h4>
        <div style="font-size:13px;display:grid;gap:8px;color:var(--text);">
          <div>• <strong>每日 08:43 / 09:43</strong>：双保险核对昨晚完场比分并推送《每日赛后复盘报告》</div>
          <div>• <strong>每日 11:43 / 12:43</strong>：双保险推送《午间焦点赛前初判简报》</div>
          <div>• <strong>每日 19:43 / 20:43</strong>：双保险推送《晚间盘口更新简报》</div>
          <div>• <strong>每日 18:13 ~ 次日 03:13（每小时）</strong>：赛前约 1 小时临场终审巡检，检测官方首发大轮换并锁单推飞书</div>
        </div>
      </div>
      <div class="box">
        <h4>⚙️ 密钥安全与本机完整控制台说明</h4>
        <div style="font-size:13px;color:var(--muted);display:grid;gap:8px;">
          <div>• 为防止飞书机器人 Webhook 密钥泄露，本云端展示页仅做安全只读展示，不存放任何密钥。</div>
          <div>• 如需修改飞书 Webhook、手动实时抓拍盘口或运行本地批量分析，请在本机双击 <strong style="color:#fff;">启动足球预测软件.bat</strong> 访问 <strong style="color:var(--accent);">http://127.0.0.1:8765</strong>。</div>
          <div style="margin-top:4px;">
            <a href="https://github.com/Felix82430/football-predictor/actions" target="_blank" rel="noopener" class="btn btn-green" style="text-decoration:none;display:inline-block;">🚀 打开云端推送后台（可一键手动触发）</a>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- 底部手机导航（手机小屏自动显示） -->
  <div class="bottom-nav">
    <button class="nav-item active" data-tab="matches" onclick="goTab('matches')"><span>⚽</span><span>赛程预测</span></button>
    <button class="nav-item" data-tab="reviews" onclick="goTab('reviews')"><span>📋</span><span>赛后复盘</span></button>
    <button class="nav-item" data-tab="backtest" onclick="goTab('backtest')"><span>📊</span><span>历史回测</span></button>
    <button class="nav-item" data-tab="settings" onclick="goTab('settings')"><span>📲</span><span>飞书设置</span></button>
  </div>

  <!-- 单场详情抽屉 -->
  <div class="modal" id="modal" onclick="if(event.target===this) closeSheet()">
    <div class="sheet" id="sheet"></div>
  </div>

  <script>
    let DATA = null;
    let curLg = 'ALL';
    let curStatus = 'SCHEDULED';
    const OUT_ZH = { H: '主胜', D: '平局', A: '客胜' };
    const LIGHT_ZH = { GREEN: '🟢 稳健一致', YELLOW: '🟡 留意分歧', RED: '🔴 高危红灯' };

    function goTab(name) {
      document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
      document.querySelectorAll('.nav-item, .top-tab').forEach(b => {
        b.classList.toggle('active', b.getAttribute('data-tab') === name);
      });
      const el = document.getElementById('tab-' + name);
      if (el) el.classList.add('active');
      window.scrollTo(0, 0);
    }

    function setStatus(st, btn) {
      curStatus = st;
      document.querySelectorAll('#statusSeg .st-btn').forEach(b => b.classList.remove('active'));
      if (btn) btn.classList.add('active');
      renderMatches();
    }

    function setLg(lg, el) {
      curLg = lg;
      document.querySelectorAll('#lgChips .chip').forEach(c => c.classList.remove('active'));
      el.classList.add('active');
      renderMatches();
    }

    function refreshStatusCounts() {
      if (!DATA) return;
      const scoped = DATA.matches.filter(m => curLg === 'ALL' || m.league_code === curLg);
      const nSched = scoped.filter(m => m.status === 'SCHEDULED').length;
      const nStart = scoped.filter(m => m.status !== 'SCHEDULED').length;
      document.getElementById('cntSched').textContent = `(${nSched})`;
      document.getElementById('cntStart').textContent = `(${nStart})`;
      document.getElementById('cntAll').textContent = `(${scoped.length})`;
    }

    async function boot() {
      const res = await fetch('./data.json?t=' + Date.now());
      DATA = await res.json();
      document.getElementById('updTime').textContent = '更新：' + DATA.updated_bjt;
      document.getElementById('kUp').textContent = DATA.overview.upcoming_matches + ' 场';
      document.getElementById('kSnap').textContent = '盘口快照 ' + DATA.overview.odds_snapshots + ' 条';

      const rs = DATA.reviews.summary;
      if (rs && rs.total > 0) {
        document.getElementById('kRevAcc').textContent = (rs.outcome_acc * 100).toFixed(1) + '%';
        const roiTxt = rs.flat_roi != null ? ` · 均注ROI ${rs.flat_roi>=0?'+':''}${rs.flat_roi}%` : '';
        document.getElementById('kRevSub').textContent = `${rs.outcome_hits}/${rs.total}场${roiTxt}`;
        const hi = (rs.tier_stats || {})['高把握(≥68分)'];
        document.getElementById('kHighAcc').textContent = hi ? `${(hi.acc*100).toFixed(0)}% (${hi.hits}/${hi.total})` : '-';
        document.getElementById('kClv').textContent = rs.double_chance_acc != null ? `防冷双选中 ${(rs.double_chance_acc*100).toFixed(0)}%` : '持续统计中';
      }
      if (DATA.backtest) {
        document.getElementById('kBt').textContent = (DATA.backtest.outcome_accuracy * 100).toFixed(1) + '%';
        document.getElementById('kBtSub').textContent = `${DATA.backtest.total_matches}场 · Top3 ${(DATA.backtest.top3_score_accuracy*100).toFixed(1)}%`;
      }

      renderMatches();
      renderReviews();
      renderBacktest();
    }

    function renderMatches() {
      if (!DATA) return;
      refreshStatusCounts();
      const sort = document.getElementById('sortSel').value;
      let list = DATA.matches.filter(m =>
        (curLg === 'ALL' || m.league_code === curLg) &&
        (curStatus === 'ALL' || (curStatus === 'STARTED' ? m.status !== 'SCHEDULED' : m.status === curStatus))
      );
      if (sort === 'confidence') {
        list.sort((a, b) => {
          const sa = a.prediction && a.prediction.confidence ? a.prediction.confidence.score : -1;
          const sb = b.prediction && b.prediction.confidence ? b.prediction.confidence.score : -1;
          return sb - sa;
        });
      } else {
        list.sort((a, b) => curStatus === 'STARTED' ? b.kickoff_utc.localeCompare(a.kickoff_utc) : a.kickoff_utc.localeCompare(b.kickoff_utc));
      }

      const grid = document.getElementById('matchGrid');
      if (!list.length) {
        grid.innerHTML = `<div class="box" style="text-align:center;color:var(--muted);">当前筛选下暂无比赛</div>`;
        return;
      }

      grid.innerHTML = list.map((m, idx) => {
        const p = m.prediction;
        const c = p && p.confidence;
        const score = m.status === 'SCHEDULED' ? 'VS' : `${m.home_score ?? '-'}:${m.away_score ?? '-'}`;
        const lot = p && p.lottery ? p.lottery : {};
        const dc = lot.double_chance || {};
        const bestTg = lot.best_total_goals_range || {};
        const ev = p && p.value_ev && p.value_ev.best_value;

        return `
          <div class="card" onclick="openSheet('${m.match_id}')">
            ${c ? `
              <div class="rank-bar">
                <span><strong style="color:#38bdf8;">${sort==='confidence'?'No.'+(idx+1)+' ':''}${c.stars}</strong> 把握度 <strong>${c.score.toFixed(0)}分</strong></span>
                <span style="color:var(--muted);font-size:11px;">${c.tier}</span>
              </div>` : ''}
            <div class="mc-meta">
              <div><span class="lg-tag">${m.league_name}</span> &nbsp;${m.kickoff_bjt}</div>
              <div>${p ? `<span class="light-${p.traffic_light}">${LIGHT_ZH[p.traffic_light] || ''}</span>` : '<span style="color:var(--muted);">待云端更新</span>'}</div>
            </div>
            <div class="teams">
              <span>${m.home_team_cn}</span>
              <span class="vs-pill">${score}</span>
              <span>${m.away_team_cn}</span>
            </div>
            ${p ? `
              <div class="prob-bar">
                <div class="pb-h" style="width:${(p.final_prob_home*100).toFixed(1)}%"></div>
                <div class="pb-d" style="width:${(p.final_prob_draw*100).toFixed(1)}%"></div>
                <div class="pb-a" style="width:${(p.final_prob_away*100).toFixed(1)}%"></div>
              </div>
              <div class="prob-txt">
                <span>主 ${(p.final_prob_home*100).toFixed(1)}%</span>
                <span>平 ${(p.final_prob_draw*100).toFixed(1)}% · 主推 <strong style="color:#fff;">${OUT_ZH[p.recommended_outcome]}</strong></span>
                <span>客 ${(p.final_prob_away*100).toFixed(1)}%</span>
              </div>
              <div class="pill-row">
                <span style="font-size:11px;color:var(--muted);">最可能比分:</span>
                ${(p.top3_scores||[]).map(x=>`<span class="pill"><strong>${x.score}</strong> ${(x.prob*100).toFixed(0)}%</span>`).join('')}
                ${bestTg.range ? `<span class="pill">总进球 <strong>${bestTg.range}</strong></span>` : ''}
              </div>
              <div class="pill-row">
                ${dc['1X'] ? `<span class="pill">${p.final_prob_home>=p.final_prob_away ? `防冷主不输 ${(dc['1X']*100).toFixed(0)}%` : `防冷客不输 ${(dc['X2']*100).toFixed(0)}%`}</span>` : ''}
                <span class="pill">预计进球 ${p.final_home_xg.toFixed(2)}:${p.final_away_xg.toFixed(2)}</span>
                ${ev ? `<span class="pill" style="border-color:#22c55e;color:#4ade80;">💎 ${ev.label}赔率有性价比</span>` : ''}
              </div>
              ${c && c.play_advice ? `<div style="font-size:11px;color:#e2e8f0;margin-top:4px;">${c.play_advice}</div>` : ''}
            ` : `<div style="font-size:12px;color:var(--muted);">云端将在赛前自动补全三层量化预测与热力图</div>`}
          </div>
        `;
      }).join('');
    }

    function renderOddsSvg(hist) {
      if (!hist || hist.length < 2) return `<div style="font-size:12px;color:var(--muted);">已抓拍 ${hist?hist.length:0} 次盘口快照</div>`;
      const w = 520, h = 135, pad = 22;
      const xs = hist.map((_, i) => pad + (i / (hist.length - 1)) * (w - pad * 2));
      const yFor = p => h - pad - p * (h - pad * 2);
      const pOf = k => hist.map((pt, i) => `${i===0?'M':'L'}${xs[i].toFixed(1)},${yFor(pt[k]||0).toFixed(1)}`).join(' ');
      return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:135px;background:#0a1120;border-radius:8px;border:1px solid var(--border);">
        <line x1="${pad}" y1="${yFor(0.5)}" x2="${w-pad}" y2="${yFor(0.5)}" stroke="#1e293b" stroke-dasharray="4 4"/>
        <path d="${pOf('home_implied')}" fill="none" stroke="#3b82f6" stroke-width="2.5"/>
        <path d="${pOf('draw_implied')}" fill="none" stroke="#94a3b8" stroke-width="2"/>
        <path d="${pOf('away_implied')}" fill="none" stroke="#f97316" stroke-width="2.5"/>
        <text x="${pad}" y="15" fill="#3b82f6" font-size="11">● 主胜</text>
        <text x="${pad+65}" y="15" fill="#94a3b8" font-size="11">● 平局</text>
        <text x="${pad+125}" y="15" fill="#f97316" font-size="11">● 客胜</text>
      </svg>`;
    }

    function openSheet(mid) {
      const m = DATA.matches.find(x => x.match_id === mid);
      if (!m) return;
      const p = m.prediction;
      const sheet = document.getElementById('sheet');
      if (!p) {
        sheet.innerHTML = `<div class="sheet-head"><strong>${m.home_team_cn} vs ${m.away_team_cn}</strong><button class="btn" onclick="closeSheet()">关闭</button></div><div class="box">该场尚未到预测窗口，云端会自动更新。</div>`;
        document.getElementById('modal').classList.add('open');
        return;
      }
      const lot = p.lottery || {};
      const rm1 = lot.handicap_minus1 || {};
      const rp1 = lot.handicap_plus1 || {};
      const dc = lot.double_chance || {};
      const tg = lot.total_goals || {};
      const hm = lot.heatmap_5x5 || [];
      const ev = p.value_ev || {};
      const ni = p.news_injuries || {};
      const l5 = ni.last_five || {};

      sheet.innerHTML = `
        <div class="sheet-head">
          <div>
            <div style="font-size:11px;color:var(--accent);">${m.league_name} · ${m.kickoff_bjt} · ${m.venue||''}</div>
            <div style="font-size:18px;font-weight:800;">${m.home_team_cn} vs ${m.away_team_cn}</div>
          </div>
          <button class="btn" onclick="closeSheet()">✕ 关闭</button>
        </div>

        <div class="box">
          <h4>🎯 综合研判（${p.confidence.stars} 把握度 ${p.confidence.score.toFixed(0)}分 · ${p.confidence.tier}）</h4>
          <div style="font-size:13px;color:#38bdf8;font-weight:700;margin-bottom:6px;">${p.confidence.play_advice || ''}</div>
          <div style="font-size:12px;color:var(--muted);margin-bottom:6px;">排序理由：${p.confidence.reason}</div>
          <div style="font-size:13px;margin-bottom:6px;">
            <strong>胜负概率：</strong>主胜 ${(p.final_prob_home*100).toFixed(1)}% / 平局 ${(p.final_prob_draw*100).toFixed(1)}% / 客胜 ${(p.final_prob_away*100).toFixed(1)}%（看好 <strong>${OUT_ZH[p.recommended_outcome]}</strong>）
          </div>
          <div style="font-size:12px;color:var(--accent);">${p.traffic_note || ''}</div>
        </div>

        <div class="box">
          <h4>🎫 常见体彩玩法概率 & 赔率性价比（看懂再参考）</h4>
          <div class="pill-row">
            ${dc['1X'] ? `<span class="pill">防冷·${m.home_team_cn}不输(胜/平) <strong>${(dc['1X']*100).toFixed(1)}%</strong></span>
            <span class="pill">防冷·${m.away_team_cn}不输(平/负) <strong>${(dc['X2']*100).toFixed(1)}%</strong></span>` : ''}
          </div>
          <div class="pill-row">
            ${rm1.win !== undefined ? `<span class="pill">让球(-1)：主赢2球+ ${(rm1.win*100).toFixed(0)}% / 刚好赢1球 ${(rm1.draw*100).toFixed(0)}% / 主不胜 ${(rm1.lose*100).toFixed(0)}%</span>` : ''}
            ${rp1.win !== undefined ? `<span class="pill">受让(+1)：主不输 ${(rp1.win*100).toFixed(0)}% / 客刚好赢1球 ${(rp1.draw*100).toFixed(0)}% / 客赢2球+ ${(rp1.lose*100).toFixed(0)}%</span>` : ''}
          </div>
          <div class="pill-row">
            ${Object.entries(tg).map(([k,v])=>`<span class="pill">全场共进${k}球 <strong>${(v*100).toFixed(0)}%</strong></span>`).join('')}
          </div>
          <div style="font-size:12px;color:#4ade80;margin:6px 0;">${ev.summary || ''}</div>
          ${hm.length ? `
            <div style="font-size:12px;color:var(--muted);margin:6px 0 4px;">所有常见比分概率表（越亮越容易踢出，横排=主队进球，竖排=客队进球）</div>
            <table>
              <thead><tr><th></th><th style="text-align:center;">客0</th><th style="text-align:center;">客1</th><th style="text-align:center;">客2</th><th style="text-align:center;">客3</th><th style="text-align:center;">客4</th></tr></thead>
              <tbody>${hm.map((row,i)=>`<tr><th>主${i}</th>${row.map(v=>`<td style="text-align:center;background:rgba(56,189,248,${Math.min(0.85,v*5.5).toFixed(2)});color:#fff;">${(v*100).toFixed(1)}%</td>`).join('')}</tr>`).join('')}</tbody>
            </table>` : ''}
        </div>

        <div class="box">
          <h4>📰 情报速览（近5场 / 体能 / 天气 / 裁判）</h4>
          <div style="font-size:12px;display:grid;gap:5px;">
            <div>• 近5场：${m.home_team_cn} [${l5.home||'暂缺'}] vs ${m.away_team_cn} [${l5.away||'暂缺'}]</div>
            <div>• 多线体能：${(p.fatigue&&p.fatigue.summary)||'-'}</div>
            <div>• 球场天气：${(p.weather&&p.weather.summary)||'-'}</div>
            <div>• 主裁判：${(p.referee&&p.referee.summary)||'-'}</div>
            ${(ni.injuries&&ni.injuries.length)?`<div>• 官方伤停：${ni.injuries.join('、')}</div>`:''}
          </div>
        </div>

        <div class="box">
          <h4>📈 盘口走势快照</h4>
          <div style="font-size:12px;margin-bottom:6px;">${p.odds_trend_note || ''}</div>
          ${renderOddsSvg(m.odds_history)}
        </div>

        ${p.media_article ? `
          <div class="box">
            <h4>✍️ 自媒体赛前长文草稿</h4>
            <textarea id="mDraft" readonly>${p.media_article}</textarea>
            <button class="btn btn-green" style="margin-top:6px;" onclick="navigator.clipboard.writeText(document.getElementById('mDraft').value).then(()=>alert('已复制到手机剪贴板！'))">📋 一键复制全文</button>
          </div>` : ''}
      `;
      document.getElementById('modal').classList.add('open');
    }

    function closeSheet() { document.getElementById('modal').classList.remove('open'); }

    function renderReviews() {
      const rs = DATA.reviews.summary;
      const items = DATA.reviews.items || [];
      if (!rs || !rs.total) {
        document.getElementById('revSummary').textContent = '暂无已完场复盘记录';
        return;
      }
      const tiers = Object.entries(rs.tier_stats || {}).map(([k,v]) =>
        `<span class="pill">${k}：<strong>${v.hits}/${v.total} (${(v.acc*100).toFixed(1)}%)</strong></span>`
      ).join(' ');
      const roiBlock = rs.flat_profit != null
        ? `<div class="pill-row" style="margin-top:6px;">
             <span class="pill" style="border-color:${rs.flat_profit>=0?'#22c55e':'#ef4444'};">💰 模拟每场均注100元：累计 <strong style="color:${rs.flat_profit>=0?'#4ade80':'#f87171'};">${rs.flat_profit>=0?'+':''}${rs.flat_profit}元 (ROI ${rs.flat_roi>=0?'+':''}${rs.flat_roi}%)</strong></span>
             ${rs.high_tier_profit != null ? `<span class="pill" style="border-color:${rs.high_tier_profit>=0?'#22c55e':'#f59e0b'};">⭐ 仅跟高把握(≥68分)：<strong style="color:${rs.high_tier_profit>=0?'#4ade80':'#fbbf24'};">${rs.high_tier_profit>=0?'+':''}${rs.high_tier_profit}元 (ROI ${rs.high_tier_roi>=0?'+':''}${rs.high_tier_roi}%)</strong></span>` : ''}
           </div>`
        : '';
      document.getElementById('revSummary').innerHTML = `
        累计复盘 <strong>${rs.total}</strong> 场 | 胜平负猜中 <strong>${rs.outcome_hits}/${rs.total} (${(rs.outcome_acc*100).toFixed(1)}%)</strong> |
        防冷双选(不输)猜中 <strong>${rs.double_chance_hits||0}/${rs.total} (${((rs.double_chance_acc||0)*100).toFixed(1)}%)</strong> |
        3个候选比分猜中 <strong>${rs.top3_hits} 场 (${(rs.top3_acc*100).toFixed(1)}%)</strong>（第1首选直接猜中 <strong>${rs.exact_top1_hits}</strong> 场）
        <div class="pill-row" style="margin-top:8px;">${tiers}</div>
        ${roiBlock}
      `;
      const attrZh = { ACCURATE: '✅ 准确猜中', VARIANCE_LUCK: '🎲 场面占优/前75分钟红牌', MODEL_OR_INTEL_MISS: '❌ 模型/情报需改进' };

      // 按比赛日期（北京时间）倒序分组，当天内严格按赛前把握度从高到低顺序排放
      const groups = {};
      items.forEach(r => {
        const day = (r.kickoff_bjt || r.reviewed_at || '未知日期').slice(0, 10);
        if (!groups[day]) groups[day] = [];
        groups[day].push(r);
      });
      const days = Object.keys(groups).sort((a, b) => b.localeCompare(a));

      document.getElementById('revCards').innerHTML = days.map(day => {
        const dayItems = groups[day].sort((a, b) => (b.confidence_score || 0) - (a.confidence_score || 0));
        const dHits = dayItems.filter(x => x.outcome_hit).length;
        const dAcc = (dHits / dayItems.length * 100).toFixed(1);
        const dHigh = dayItems.filter(x => (x.confidence_score || 0) >= 68);
        const dHighHits = dHigh.filter(x => x.outcome_hit).length;
        const dProfitArr = dayItems.filter(x => x.flat_profit != null);
        const dProfit = dProfitArr.length ? dProfitArr.reduce((s, x) => s + x.flat_profit, 0) : null;

        return `
          <div class="day-group">
            <div class="day-head">
              <span>📅 ${day} 赛后复盘（按当天赛前把握度从高到低）</span>
              <span style="font-size:12px;font-weight:600;color:var(--accent);">
                当日命中 <strong>${dHits}/${dayItems.length} (${dAcc}%)</strong>
                ${dHigh.length ? ` · 高把握 ${dHighHits}/${dHigh.length}` : ''}
                ${dProfit != null ? ` · 均注100元 <strong style="color:${dProfit>=0?'#4ade80':'#f87171'};">${dProfit>=0?'+':''}${dProfit.toFixed(1)}元</strong>` : ''}
              </span>
            </div>
            ${dayItems.map((r, idx) => `
              <div class="box">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;gap:6px;flex-wrap:wrap;">
                  <div>
                    <span class="lg-tag">No.${idx+1} · 把握 ${(r.confidence_score||0).toFixed(0)}分</span>
                    <strong style="margin-left:6px;">[${r.league_name}] ${r.home_team_cn} ${r.actual_home_score}-${r.actual_away_score} ${r.away_team_cn}</strong>
                  </div>
                  <span style="font-size:12px;font-weight:700;color:${r.outcome_hit?'#4ade80':'#fbbf24'};">${attrZh[r.attribution_type]||r.attribution_type}</span>
                </div>
                <div style="font-size:12px;color:var(--muted);margin-bottom:4px;">
                  ${r.kickoff_bjt || ''} · 主推 ${OUT_ZH[r.recommended_outcome]}${r.used_odds ? ` @${r.used_odds}` : ''}
                  ${r.flat_profit != null ? ` · 均注100元 <strong style="color:${r.flat_profit>=0?'#4ade80':'#f87171'};">${r.flat_profit>=0?'+':''}${r.flat_profit}元</strong>` : ''}
                  · Top3: ${(r.top3_scores||[]).map(x=>x.score).join('/')} ${r.top3_score_hit?'🎯中Top3':''}
                </div>
                <div style="font-size:12px;">${r.root_cause_analysis}</div>
                <div style="font-size:11px;color:var(--accent);margin-top:4px;">建议：${r.tuning_suggestion||'-'}</div>
              </div>
            `).join('')}
          </div>
        `;
      }).join('');
    }

    function renderBacktest() {
      const bt = DATA.backtest;
      if (!bt) { document.getElementById('btWrap').innerHTML = '<div class="box">暂无回测数据</div>'; return; }
      const lgs = Object.values((bt.details && bt.details.league_summaries) || {});
      document.getElementById('btWrap').innerHTML = `
        <div class="box">
          <h4>🏆 五大联赛近 45 天无作弊滚动回测（${bt.date_from} ~ ${bt.date_to}，共 ${bt.total_matches} 场）</h4>
          <div class="pill-row">
            <span class="pill">数理底盘命中 <strong>${(bt.outcome_accuracy*100).toFixed(1)}%</strong></span>
            <span class="pill">机构赔率基准 <strong>${bt.market_accuracy?(bt.market_accuracy*100).toFixed(1)+'%':'-'}</strong></span>
            <span class="pill">Top3比分命中 <strong>${(bt.top3_score_accuracy*100).toFixed(1)}%</strong></span>
          </div>
        </div>
        <div class="box">
          <h4>🌍 分联赛回测明细</h4>
          <table>
            <thead><tr><th>联赛</th><th>样本</th><th>胜平负命中</th><th>Top3命中</th><th>Brier</th></tr></thead>
            <tbody>${lgs.map(x=>`<tr><td>${x.league_name}</td><td>${x.total}</td><td><strong>${(x.outcome_acc*100).toFixed(1)}%</strong></td><td>${(x.top3_acc*100).toFixed(1)}%</td><td>${x.brier}</td></tr>`).join('')}</tbody>
          </table>
        </div>
      `;
    }

    boot();
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    print(json.dumps(export_static_site(), ensure_ascii=False, indent=2))
