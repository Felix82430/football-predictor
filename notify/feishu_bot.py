"""
飞书自定义机器人推送模块（支持 Interactive Card 交互卡片 + 可选签名校验）
支持三种核心卡片场景：
1. 每日焦点赛程《初判简报》汇总卡片（一段推送）
2. 单场《临场首发与盘口终审》重点预警卡片（二段推送）
3. 《每日赛后核对与复盘报告》卡片（赛后闭环推送）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from config import load_settings


def _gen_sign(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def send_feishu_card(
    card_content: Dict[str, Any],
    webhook_url: Optional[str] = None,
    secret: Optional[str] = None,
) -> Dict[str, Any]:
    """向飞书自定义机器人发送 Interactive Card（优先读取云端环境变量，其次读取本地设置）"""
    settings = load_settings()
    url = (
        webhook_url
        or os.environ.get("FEISHU_WEBHOOK")
        or settings.get("feishu_webhook")
        or ""
    ).strip()
    sec = (
        secret
        if secret is not None
        else (os.environ.get("FEISHU_SECRET") or settings.get("feishu_secret") or "")
    ).strip()

    if not url:
        return {"ok": False, "error": "尚未配置飞书机器人 Webhook 地址，请先在网页【设置】中填写。"}

    payload: Dict[str, Any] = {
        "msg_type": "interactive",
        "card": card_content,
    }
    if sec:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _gen_sign(sec, ts)

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload)
            data = resp.json()
            if resp.status_code == 200 and data.get("code", 0) == 0:
                return {"ok": True, "response": data}
            return {"ok": False, "error": f"飞书返回异常：{data}"}
    except Exception as e:
        return {"ok": False, "error": f"飞书推送失败：{e}"}


def build_daily_brief_card(items: List[Dict[str, Any]], title_suffix: str = "每日焦点初判简报") -> Dict[str, Any]:
    """构建多场次汇总交互卡片（手机端清爽速览）"""
    elements: List[Dict[str, Any]] = []
    light_icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}
    out_label = {"H": "主胜", "D": "平局", "A": "客胜"}

    for idx, entry in enumerate(items[:10]):
        m = entry["match"]
        p = entry["prediction"]
        top3 = json.loads(p.get("top3_scores_json") or "[]")
        top3_str = " / ".join(f"{x['score']}({x['prob']*100:.0f}%)" for x in top3)
        icon = light_icon.get(p.get("traffic_light", "GREEN"), "🟢")
        rec = out_label.get(p.get("recommended_outcome", "H"), "主胜")
        lock_tag = "🔒已锁盘" if p.get("is_locked") else "初判"

        md = (
            f"**{idx+1}. [{m['league_name']}] {m['home_team_cn']} vs {m['away_team_cn']}**  `{m['kickoff_bjt']}`  {icon} {lock_tag}\n"
            f"• **概率**：主胜 `{p['final_prob_home']*100:.1f}%` | 平 `{p['final_prob_draw']*100:.1f}%` | 客胜 `{p['final_prob_away']*100:.1f}%`  →  **主推：{rec}**\n"
            f"• **Top 3 比分**：{top3_str}（预期进球 `{p['final_home_xg']:.2f}:{p['final_away_xg']:.2f}`）\n"
            f"• **动态修正**：主 `{p['home_intel_delta']*100:+.1f}%` / 客 `{p['away_intel_delta']*100:+.1f}%` | {p.get('traffic_note', '')}"
        )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": md}})
        if idx < len(items[:10]) - 1:
            elements.append({"tag": "hr"})

    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": "提示：开赛前1小时可在电脑端/对话中触发【二段临场终审】核对官方首发与最终盘口。理性娱乐，切勿盲从。",
                }
            ],
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"⚽ 足球量化情报官 · {title_suffix}"},
            "template": "blue",
        },
        "elements": elements,
    }


def build_single_final_card(match: Dict[str, Any], pred: Dict[str, Any]) -> Dict[str, Any]:
    """构建单场《临场终审》详细卡片（含加减分明细账）"""
    top3 = json.loads(pred.get("top3_scores_json") or "[]")
    intel_items = json.loads(pred.get("intel_items_json") or "[]")
    light = pred.get("traffic_light", "GREEN")
    header_color = {"GREEN": "green", "YELLOW": "orange", "RED": "red"}.get(light, "blue")
    out_zh = {"H": f"主胜（{match['home_team_cn']}）", "D": "平局", "A": f"客胜（{match['away_team_cn']}）"}.get(
        pred["recommended_outcome"], ""
    )

    intel_lines = []
    for it in intel_items:
        intel_lines.append(
            f"- **{it.get('label')}**（主`{it.get('home_delta',0)*100:+.1f}%` / 客`{it.get('away_delta',0)*100:+.1f}%`）：{it.get('reason')}"
        )
    intel_md = "\n".join(intel_lines) if intel_lines else "- 未触发额外动态加减分（纯数理底盘）"

    body = (
        f"**赛事**：[{match['league_name']}] {match['home_team_cn']} vs {match['away_team_cn']}\n"
        f"**开球（北京时间）**：{match['kickoff_bjt']} | **阶段**：{'临场终审 🔒' if pred.get('stage')=='FINAL' else '赛前初判'}\n\n"
        f"**📊 最终胜平负概率**\n"
        f"主胜 `{pred['final_prob_home']*100:.1f}%` | 平局 `{pred['final_prob_draw']*100:.1f}%` | 客胜 `{pred['final_prob_away']*100:.1f}%`\n"
        f"**🎯 主推方向**：**{out_zh}**\n"
        f"**🔢 Top 3 比分**：" + " / ".join(f"**{x['score']}**({x['prob']*100:.1f}%)" for x in top3) + "\n"
        f"**⚽ 预期进球**：`{pred['final_home_xg']:.2f} : {pred['final_away_xg']:.2f}`（大2.5球 `{pred.get('over_25_prob',0)*100:.1f}%`）\n\n"
        f"**🧾 情报加减分明细账**\n{intel_md}\n\n"
        f"**📈 盘口与红绿灯体检**\n{pred.get('odds_trend_note','')}\n{pred.get('traffic_note','')}"
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"⚽ {'临场终审' if pred.get('stage')=='FINAL' else '深度研报'}：{match['home_team_cn']} vs {match['away_team_cn']}",
            },
            "template": header_color,
        },
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
    }


def build_review_report_card(summary: Dict[str, Any], reviews: List[Dict[str, Any]]) -> Dict[str, Any]:
    """构建《每日赛后核对与复盘报告》飞书卡片"""
    header_md = (
        f"**累计核对场次**：`{summary['total']}` 场\n"
        f"**胜平负命中率**：`{summary['outcome_acc']*100:.1f}%`（{summary['outcome_hits']}/{summary['total']}）\n"
        f"**Top 3 比分命中**：`{summary['top3_acc']*100:.1f}%`（{summary['top3_hits']}/{summary['total']}，首选精准 `{summary['exact_top1_hits']}` 场）\n"
        f"**误差归因分布**：偶然红牌/运气 `{summary['luck_misses']}` 场 | 模型/情报待调优 `{summary['model_misses']}` 场 | 红黄灯避坑 `{summary['upset_saves']}` 次"
    )

    elements: List[Dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": header_md}},
        {"tag": "hr"},
    ]

    for r in reviews[:8]:
        icon = "✅" if r["outcome_hit"] else ("🎲" if r["attribution_type"] == "VARIANCE_LUCK" else "❌")
        top3_tag = "🎯命中Top3" if r["top3_score_hit"] else ""
        md = (
            f"{icon} **[{r['league_name']}] {r['home_team_cn']} {r['actual_home_score']}-{r['actual_away_score']} {r['away_team_cn']}** {top3_tag}\n"
            f"• 复盘：{r['root_cause_analysis']}\n"
            f"• 改进建议：{r.get('tuning_suggestion', '')}"
        )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": md}})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📋 每日赛后核对与归因复盘报告"},
            "template": "purple",
        },
        "elements": elements,
    }
