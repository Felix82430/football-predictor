"""
自媒体写稿一键生成器（面向未来公众号 / 今日头条 / 懂球帝 / 小红书等图文输出）
特点：有数据、有逻辑、讲人话、透明亮出加减分账单，自带理性看球合规免责声明。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List


def generate_media_article(
    match: Dict[str, Any],
    pred: Dict[str, Any],
    home_elo: float = 1600.0,
    away_elo: float = 1600.0,
    starters: Dict[str, List[str]] | None = None,
) -> str:
    h_cn = match.get("home_team_cn") or match["home_team"]
    a_cn = match.get("away_team_cn") or match["away_team"]
    lg = match.get("league_name") or match["league_code"]
    kickoff = match.get("kickoff_bjt") or match.get("kickoff_utc")

    top3 = json.loads(pred.get("top3_scores_json") or "[]")
    intel_items = json.loads(pred.get("intel_items_json") or "[]")
    weather = json.loads(pred.get("weather_json") or "{}")
    referee = json.loads(pred.get("referee_json") or "{}")
    fatigue = json.loads(pred.get("fatigue_json") or "{}")

    ph = pred["final_prob_home"] * 100
    pd_ = pred["final_prob_draw"] * 100
    pa = pred["final_prob_away"] * 100
    rec_map = {"H": f"{h_cn} 主场不败/主胜方向", "D": "双方握手言和（防平局）", "A": f"{a_cn} 客场抢分/客胜方向"}
    rec_text = rec_map.get(pred["recommended_outcome"], "均衡格局")

    light_map = {"GREEN": "🟢 稳健共振（与市场观点一致）", "YELLOW": "🟡 存在分歧（重点留意冷门/价值）", "RED": "🔴 红灯高警（分歧过大，严防临场变数）"}
    light_text = light_map.get(pred.get("traffic_light", "GREEN"), "🟢 稳健")

    lines = [
        f"# 【{lg}深度前瞻】{h_cn} vs {a_cn}：数据底盘+情报加减分全拆解！",
        "",
        f"> **开球时间（北京时间）**：{kickoff} | **球场**：{match.get('venue') or '主场'}",
        f"> **风险体检灯**：{light_text}",
        "",
        "---",
        "",
        "## 一、先看硬核数据底盘（不带感情色彩）",
        f"- **全欧 ELO 战力对比**：{h_cn} `{home_elo:.0f}` vs {a_cn} `{away_elo:.0f}`（主场加成后差距清晰可见）。",
        f"- **Dixon-Coles + 预期进球(xG) 基准模型**：在不考虑临场突发新闻前，双方原始预期进球为 **{pred['base_home_xg']:.2f} : {pred['base_away_xg']:.2f}**，初始胜平负概率为 **主胜 {pred['base_prob_home']*100:.1f}% / 平局 {pred['base_prob_draw']*100:.1f}% / 客胜 {pred['base_prob_away']*100:.1f}%**。",
        "",
        "## 二、赛前动态情报「加减分账单」",
        f"- **多线作战与体能休息差**：{fatigue.get('summary') or '正常赛程间隔'}",
        f"- **球场天气与裁判尺度**：{weather.get('summary') or '正常天气'}；{referee.get('summary') or '主裁判待公布'}",
    ]

    if intel_items:
        lines.append("")
        lines.append("**本场触发的具体修正项（每项均设上限，杜绝拍脑袋）：**")
        for item in intel_items:
            hd = item.get("home_delta", 0.0) * 100
            ad = item.get("away_delta", 0.0) * 100
            lines.append(
                f"- **{item.get('label', item.get('category'))}**（{h_cn} `{hd:+.1f}%` / {a_cn} `{ad:+.1f}%`）：{item.get('reason')}"
            )
    else:
        lines.append("- **临场重磅伤停/战意修正**：暂未录入额外人工修正项（可结合赛前1小时官宣首发进一步重算）。")

    if starters:
        lines.append("")
        lines.append("## 三、临场官方首发速览")
        for tname, plist in starters.items():
            lines.append(f"- **{tname}**：{', '.join(plist[:11])}")

    lines.extend(
        [
            "",
            "## 盘口走势与市场体检",
            f"- {pred.get('odds_trend_note') or '持续跟踪盘口快照中'}",
            f"- **体检结论**：{pred.get('traffic_note') or ''}",
            "",
            "## 综合研判与 Top 3 比分参考",
            f"- **修正后最终预期进球**：{h_cn} `{pred['final_home_xg']:.2f}` : `{pred['final_away_xg']:.2f}` {a_cn}",
            f"- **最终概率分布**：主胜 **{ph:.1f}%** | 平局 **{pd_:.1f}%** | 客胜 **{pa:.1f}%**",
            f"- **模型主推倾向**：{rec_text}",
            f"- **最可能 Top 3 比分**：" + " / ".join(f"**{x['score']}** ({x['prob']*100:.1f}%)" for x in top3),
            f"- **大2.5球概率**：`{(pred.get('over_25_prob') or 0)*100:.1f}%` | **双方均有进球(BTTS)**：`{(pred.get('btts_prob') or 0)*100:.1f}%`",
            "",
            "---",
            "*免责声明：本文基于公开赛事统计数据、预期进球模型与赛前公开资讯整理分析，仅供体育赛事交流与看球参考，足球赛场瞬息万变，请理性观赛，切勿沉迷。*",
        ]
    )
    return "\n".join(lines)
