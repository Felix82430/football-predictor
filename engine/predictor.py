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
    DATA_DIR,
    INTEL_LIMITS,
    MARKET_TRAFFIC_LIGHT,
    TOTAL_INTEL_CAP,
    load_settings,
)
from data_sources.espn_client import fetch_event_summary
from data_sources.football_data_client import (
    build_referee_profiles,
    load_all_top5_history,
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
from engine.market_math import compute_ev_and_kelly, invert_market_to_expected_goals

MANUAL_INTEL_PATH = DATA_DIR / "manual_intel.json"
LAST_STARTERS_PATH = DATA_DIR / "last_starters.json"


def _load_last_starters() -> Dict[str, List[str]]:
    if not LAST_STARTERS_PATH.exists():
        return {}
    try:
        return json.loads(LAST_STARTERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_last_starters(data: Dict[str, List[str]]) -> None:
    LAST_STARTERS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def detect_lineup_rotation(
    home_team: str,
    away_team: str,
    home_cn: str,
    away_cn: str,
    current_starters: Dict[str, List[str]],
) -> Optional[Dict[str, Any]]:
    """
    v1.6 0成本临场首发轮换自动检测（Lineup Stability）：
    将赛前1小时官宣的 11 人首发与球队上一场 11 人首发自动对比；
    若单队轮换 >= 4 名主力，自动扣减攻防修正分并生成大白话预警！
    """
    if not current_starters:
        return None
    saved = _load_last_starters()
    h_delta = 0.0
    a_delta = 0.0
    notes: List[str] = []
    updated = False

    for team, cn, is_home in ((home_team, home_cn, True), (away_team, away_cn, False)):
        cur_list = current_starters.get(team) or []
        if len(cur_list) < 9:
            continue
        prev_list = saved.get(team) or []
        if len(prev_list) >= 9:
            prev_set = set(prev_list)
            cur_set = set(cur_list)
            changed = len(cur_set - prev_set)
            if changed >= 4:
                pen = -min(0.10, round((changed - 3) * 0.025, 3))
                if is_home:
                    h_delta = pen
                else:
                    a_delta = pen
                notes.append(f"⚠️ {cn}临场大轮换（相比上场换了 {changed} 名主力，小心留力或磨合不足）")
        if cur_list != prev_list:
            saved[team] = cur_list[:11]
            updated = True

    if updated:
        _save_last_starters(saved)

    if not notes:
        return None
    return {
        "category": "injuries_lineup",
        "home_delta": h_delta,
        "away_delta": a_delta,
        "reason": "；".join(notes),
        "source_name": "自动临场首发轮换对比",
        "source_url": "",
    }


def load_manual_intel(match_id: str) -> List[Dict[str, Any]]:
    """从纯文本 JSON 读取人工/AI 情报账单（彻底避免本地与云端二进制数据库合并冲突）"""
    if not MANUAL_INTEL_PATH.exists():
        return []
    try:
        data = json.loads(MANUAL_INTEL_PATH.read_text(encoding="utf-8"))
        return list(data.get(match_id) or [])
    except Exception:
        return []


def save_manual_intel(match_id: str, items: List[Dict[str, Any]]) -> None:
    data: Dict[str, Any] = {}
    if MANUAL_INTEL_PATH.exists():
        try:
            data = json.loads(MANUAL_INTEL_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if items:
        data[match_id] = items
    else:
        data.pop(match_id, None)
    MANUAL_INTEL_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class LeagueEngineCache:
    """缓存当前进程内的各联赛历史表、拟合好的 Dixon-Coles 模型、xG 与裁判档案，避免重复计算"""

    def __init__(self) -> None:
        self.all_df: Optional[pd.DataFrame] = None
        self.euro_df: Optional[pd.DataFrame] = None
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

        # 五大联赛用于 Dixon-Coles 模型与裁判档案；叠加 6 大欧洲二线联赛（荷甲/葡超/比甲/土超/苏超/希超）用于全欧 ELO
        self.all_df = load_all_top5_history(force_refresh=force_refresh, include_euro_secondary=False)
        self.euro_df = load_all_top5_history(force_refresh=force_refresh, include_euro_secondary=True)
        self.elo_map = compute_elo_ratings(self.euro_df if not self.euro_df.empty else self.all_df)
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
    """第三层：市场赔率红绿灯防偏体检（全大白话输出，让所有球迷一眼看懂）"""
    if any(x is None for x in market_probs):
        return "YELLOW", "暂时还没抓到全球主流赔率做对比，建议临开球前再看一眼最新变化。"

    diffs = [abs(f - m) for f, m in zip(final_probs, market_probs)]  # type: ignore
    max_diff = max(diffs)
    diff_pct = round(max_diff * 100, 1)

    if abnormal_odds_shift and max_diff > MARKET_TRAFFIC_LIGHT["green_max_diff"]:
        return "RED", f"临场赔率突然大变，且和模型看法相差 {diff_pct}%！可能有突发伤病或大资金异动，一定要等赛前1小时看首发，别盲目重注。"

    if max_diff <= MARKET_TRAFFIC_LIGHT["green_max_diff"]:
        return "GREEN", f"数据模型和全球主流赔率看法高度一致（相差仅 {diff_pct}%），没有明显暗雷，参考价值高。"

    if max_diff <= MARKET_TRAFFIC_LIGHT["yellow_max_diff"]:
        if has_strong_intel:
            return "YELLOW", f"模型和赔率有 {diff_pct}% 的分歧，但我们查到了伤停/赛程理由支撑，留意冷门或性价比机会。"
        return "YELLOW", f"模型和赔率有 {diff_pct}% 的分歧，暂时没查到重磅伤停解释，稳妥起见优先防平或等赛前1小时看首发。"

    return "RED", f"模型和全球赔率分歧太大（相差 {diff_pct}%）！可能存在没公开的伤病或放水战意，极易爆冷，建议看热闹别重注。"


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


def compute_confidence_score(pred: Dict[str, Any]) -> Dict[str, Any]:
    """
    计算单场综合把握度评分（0~100分）与推荐星级，用于每日比赛从高到低智能排序。
    公式完全透明，便于赛后复盘检验与持续优化：
    1. 胜率与领先优势（最高45分）：主推方向概率越高、领先次优方向差距越大，分越高
    2. 市场红绿灯共振（最高30分，红灯额外扣10分）：绿灯共振加满分，红灯强行压到榜尾防踩雷
    3. 数据与情报完整度（最高15分）：有赔率对照、盘口平稳、有情报核查加分
    4. 预期进球净优势（最高10分）：预期进球差越明显，低比分偶然翻车概率越低
    """
    probs = sorted(
        [
            float(pred.get("final_prob_home") or 0.0),
            float(pred.get("final_prob_draw") or 0.0),
            float(pred.get("final_prob_away") or 0.0),
        ],
        reverse=True,
    )
    top_p, second_p = probs[0], probs[1]
    margin = max(0.0, top_p - second_p)

    # 1. 胜率与领先优势（0~45分）
    prob_part = min(28.0, max(0.0, (top_p - 0.33) / 0.42 * 28.0))
    margin_part = min(17.0, max(0.0, margin / 0.40 * 17.0))
    s_prob = round(prob_part + margin_part, 1)

    # 2. 市场红绿灯共振（-10 ~ +30分）
    light = pred.get("traffic_light") or "YELLOW"
    has_intel = abs(float(pred.get("home_intel_delta") or 0.0)) >= 0.03 or abs(
        float(pred.get("away_intel_delta") or 0.0)
    ) >= 0.03
    if light == "GREEN":
        s_market = 30.0
        mkt_reason = "与市场赔率高度共振(绿灯)"
    elif light == "YELLOW":
        s_market = 16.0 if has_intel else 12.0
        mkt_reason = "与市场轻度分歧(黄灯)"
    else:
        s_market = -10.0
        mkt_reason = "与市场分歧过大或异常变盘(红灯降权)"

    # 3. 数据与情报完整度（0~15分）
    s_data = 0.0
    if pred.get("market_prob_home") is not None:
        s_data += 8.0
    odds_note = pred.get("odds_trend_note") or ""
    if "异常变盘" not in odds_note and "暂无公开赔率" not in odds_note:
        s_data += 4.0
    if has_intel or pred.get("stage") == "FINAL":
        s_data += 3.0

    # 4. 预期进球净优势（0~10分）
    xg_diff = abs(float(pred.get("final_home_xg") or 0.0) - float(pred.get("final_away_xg") or 0.0))
    s_xg = round(min(10.0, xg_diff / 1.4 * 10.0), 1)

    total = round(max(5.0, min(99.0, s_prob + s_market + s_data + s_xg)), 1)

    if total >= 80:
        stars, tier = "⭐⭐⭐⭐⭐", "极高把握·核心重点"
    elif total >= 68:
        stars, tier = "⭐⭐⭐⭐", "较高把握·稳健参考"
    elif total >= 55:
        stars, tier = "⭐⭐⭐", "中等把握·理性参考"
    elif total >= 40:
        stars, tier = "⭐⭐", "把握偏低·建议防冷或观望"
    else:
        stars, tier = "⭐", "高危红灯·建议避开"

    # 大白话排序理由（让不看专业术语的普通球迷一眼秒懂为什么排在第几名）
    plain_reasons = []
    if s_prob >= 28:
        plain_reasons.append("两队实力差距很明显")
    elif s_prob >= 16:
        plain_reasons.append("强弱有一定差距")
    else:
        plain_reasons.append("两队实力比较接近、容易胶着")

    if light == "GREEN":
        plain_reasons.append("和全球主流赔率看法一致")
    elif light == "YELLOW":
        plain_reasons.append("和赔率略有分歧(建议看首发)")
    else:
        plain_reasons.append("和赔率分歧太大或异常变盘(已自动降排)")

    if s_xg >= 6.5:
        plain_reasons.append("预计净胜球优势大")

    # v1.6 大白话实战策略标签（结合昨晚绿灯5/5、黄灯防平3/3规律）
    rec_code = pred.get("recommended_outcome") or "H"
    side_word = "主胜" if rec_code == "H" else ("客胜" if rec_code == "A" else "平局")
    dc_word = "主队不输(胜/平)" if float(pred.get("final_prob_home") or 0) >= float(pred.get("final_prob_away") or 0) else "客队不输(平/负)"

    if light == "GREEN" and total >= 68:
        play_advice = f"🔥 稳胆思路：可优先看{side_word}（求稳防冷可选「{dc_word}」）"
    elif light == "GREEN":
        play_advice = f"⚖️ 势均力敌：数据靠谱但实力接近，单挑{side_word}有风险，稳妥优先看「{dc_word}」"
    elif light == "YELLOW":
        play_advice = f"🛡️ 防平优先：数据与赔率有分歧，别盲目单挑独赢，建议等赛前1小时看首发或选「{dc_word}」"
    else:
        play_advice = "🚫 高危观望：分歧过大或突然变盘，极易爆冷，建议只看球别重注"

    reason = " + ".join(plain_reasons)
    return {
        "score": total,
        "stars": stars,
        "tier": tier,
        "reason": reason,
        "play_advice": play_advice,
        "breakdown": {
            "prob_score": s_prob,
            "market_score": s_market,
            "data_score": s_data,
            "xg_score": s_xg,
        },
    }


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

    # 先提前读取赔率快照（供欧冠小联赛贝叶斯市场锚定、EV价值计算与红绿灯体检复用）
    odds_history = get_odds_history(match["match_id"])
    odds_analysis = analyze_odds_trend(odds_history)
    latest_odds_row = odds_history[-1] if odds_history else {}

    # ========== 第 1 层：70% 客观数理底盘 ==========
    lam_h, lam_a, rho = None, None, -0.06
    dc_model = ENGINE_CACHE.league_models.get(league_code)
    if dc_model and dc_model.is_fitted:
        eg = dc_model.predict_expected_goals(home, away)
        if eg:
            lam_h, lam_a = eg
            rho = dc_model.rho

    # 欧冠跨联赛或升班马未命中单联赛模型时，用全欧11国联赛 ELO + 市场贝叶斯锚定估算（彻底消灭默认1580克隆人Bug！）
    has_h_elo = home in ENGINE_CACHE.elo_map
    has_a_elo = away in ENGINE_CACHE.elo_map
    home_elo = ENGINE_CACHE.elo_map.get(home, 1460.0)
    away_elo = ENGINE_CACHE.elo_map.get(away, 1460.0)
    cross_league_anchored = False

    if lam_h is None or lam_a is None:
        elo_lam_h, elo_lam_a = elo_to_expected_goals(home_elo, away_elo)
        # 若有市场赔率且存在缺失 ELO 的非11国冷门球队（如奥甲/挪超/阿塞拜疆等），启用贝叶斯市场锚定融合
        if odds_analysis["market_prob_home"] is not None:
            mkt_eg = invert_market_to_expected_goals(
                (
                    odds_analysis["market_prob_home"],
                    odds_analysis["market_prob_draw"],
                    odds_analysis["market_prob_away"],
                ),
                over_under_line=latest_odds_row.get("over_under_line"),
                over_odds=latest_odds_row.get("over_odds"),
                under_odds=latest_odds_row.get("under_odds"),
            )
            if mkt_eg:
                if not has_h_elo and not has_a_elo:
                    anchor_w = 0.80  # 两队均无国内联赛数据 -> 80% 锚定市场实力基准
                elif not has_h_elo or not has_a_elo:
                    anchor_w = 0.60  # 一队缺失 -> 60% 锚定市场，40% 保留已知强队 ELO
                else:
                    anchor_w = 0.30  # 两队均有真实欧洲联赛 ELO -> 仅轻度 30% 跨联赛校准
                lam_h = round((1.0 - anchor_w) * elo_lam_h + anchor_w * mkt_eg[0], 3)
                lam_a = round((1.0 - anchor_w) * elo_lam_a + anchor_w * mkt_eg[1], 3)
                cross_league_anchored = True
            else:
                lam_h, lam_a = elo_lam_h, elo_lam_a
        else:
            lam_h, lam_a = elo_lam_h, elo_lam_a

    # 融合预期进球（Understat xG / 射门拟合 xG），戳穿“纯靠运气连胜”假象
    h_xg_info = ENGINE_CACHE.get_team_xg(league_code, home)
    a_xg_info = ENGINE_CACHE.get_team_xg(league_code, away)
    if h_xg_info and a_xg_info:
        xg_lam_h = 0.5 * (h_xg_info["home_xg_for"] + a_xg_info["away_xg_against"])
        xg_lam_a = 0.5 * (a_xg_info["away_xg_for"] + h_xg_info["home_xg_against"])
        xg_lam_h = 0.65 * xg_lam_h + 0.35 * (0.5 * (h_xg_info["recent6_xg_for"] + a_xg_info["recent6_xg_against"]))
        xg_lam_a = 0.65 * xg_lam_a + 0.35 * (0.5 * (a_xg_info["recent6_xg_for"] + h_xg_info["recent6_xg_against"]))

        lam_h = round((1.0 - xg_blend) * lam_h + xg_blend * xg_lam_h, 3)
        lam_a = round((1.0 - xg_blend) * lam_a + xg_blend * xg_lam_a, 3)

    base_matrix = build_score_matrix(lam_h, lam_a, rho=rho)

    # ========== 第 2 层：动态情报修正（自动触发器 + 纯文本防冲突人工/AI情报账单） ==========
    with get_conn() as conn:
        recent_db = [dict(r) for r in conn.execute("SELECT home_team, away_team, kickoff_utc, status FROM matches WHERE status='FINISHED'").fetchall()]
    fatigue_info = compute_rest_and_fatigue(
        home, away, match["kickoff_utc"], ENGINE_CACHE.euro_df if ENGINE_CACHE.euro_df is not None else ENGINE_CACHE.all_df, recent_db
    )

    weather_info = fetch_match_weather(home, away, league_code, match["kickoff_utc"])
    referee_info = evaluate_referee_trigger(referee_name, ENGINE_CACHE.ref_profiles)

    # 若外部未显式传 ai_intel_items，自动加载纯文本防冲突账单 manual_intel.json
    effective_manual = ai_intel_items if ai_intel_items is not None else load_manual_intel(match["match_id"])
    combined_items: List[Dict[str, Any]] = list(effective_manual or [])
    existing_cats = {x.get("category") for x in combined_items}

    # v1.6 自动检测临场官宣首发是否发生大轮换（0成本对比连续两场首发名单）
    if "injuries_lineup" not in existing_cats and live_summary.get("starters"):
        rot_item = detect_lineup_rotation(
            home, away, match["home_team_cn"], match["away_team_cn"], live_summary["starters"]
        )
        if rot_item:
            combined_items.append(rot_item)
            existing_cats.add("injuries_lineup")

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

    # ========== 第 3 层：盘口走势 + 红绿灯体检 + EV价值评估 + 综合把握度评分 ==========
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
    if cross_league_anchored:
        traffic_note = f"（已结合跨联赛赔率校准）{traffic_note}"

    value_ev = compute_ev_and_kelly(
        (final_matrix["prob_home"], final_matrix["prob_draw"], final_matrix["prob_away"]),
        (
            latest_odds_row.get("home_odds"),
            latest_odds_row.get("draw_odds"),
            latest_odds_row.get("away_odds"),
        ),
    )

    news_injuries = {
        "injuries": live_summary.get("injuries") or [],
        "news_headlines": live_summary.get("news_headlines") or [],
        "last_five": {
            "home": (live_summary.get("last_five") or {}).get(home, ""),
            "away": (live_summary.get("last_five") or {}).get(away, ""),
        },
        "starters": live_summary.get("starters") or {},
    }

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
        "model_version": settings.get("model_version", "v1.5-量化体彩版"),
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
        "lottery_json": json.dumps(final_matrix.get("lottery_markets") or {}, ensure_ascii=False),
        "value_ev_json": json.dumps(value_ev, ensure_ascii=False),
        "news_injuries_json": json.dumps(news_injuries, ensure_ascii=False),
    }

    conf = compute_confidence_score(pred_record)
    pred_record["confidence_score"] = conf["score"]
    pred_record["confidence_tier"] = conf["tier"]

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
