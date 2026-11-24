"""
专业体育金融与市场精算模块（参考 GitHub 开源项目 footquant-arbitrage / value-bet-model）
包含：
1. Power 幂次去水法（修正机构对高赔冷门方抽水偏重的偏差，优于简单等比归一化）
2. 期望值 EV 与 1/4 凯利理性娱乐参考比例（严格封顶，防上头）
3. 市场隐含预期进球反推（解决欧冠冷门非五大联赛球队无历史样本时的贝叶斯基准锚定）
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

from scipy.optimize import brentq, minimize


def power_devig(home_odds: float, draw_odds: float, away_odds: float) -> Tuple[float, float, float]:
    """
    Power Method（幂次去水法）：
    寻找指数 k > 0，使得 (1/O_H)^k + (1/O_D)^k + (1/O_A)^k = 1。
    相比简单等比归一化，Power 法更能准确还原冷门高赔方的真实隐含概率。
    """
    if min(home_odds, draw_odds, away_odds) <= 1.01:
        inv = [1.0 / max(home_odds, 1.01), 1.0 / max(draw_odds, 1.01), 1.0 / max(away_odds, 1.01)]
        s = sum(inv)
        return round(inv[0] / s, 4), round(inv[1] / s, 4), round(inv[2] / s, 4)

    raw = (1.0 / home_odds, 1.0 / draw_odds, 1.0 / away_odds)
    overround = sum(raw)
    if overround <= 1.0001:
        s = overround
        return round(raw[0] / s, 4), round(raw[1] / s, 4), round(raw[2] / s, 4)

    def f(k: float) -> float:
        return (raw[0] ** k) + (raw[1] ** k) + (raw[2] ** k) - 1.0

    try:
        k_star = brentq(f, 0.5, 5.0, xtol=1e-6)
        probs = [p ** k_star for p in raw]
        s = sum(probs)
        return round(probs[0] / s, 4), round(probs[1] / s, 4), round(probs[2] / s, 4)
    except Exception:
        s = overround
        return round(raw[0] / s, 4), round(raw[1] / s, 4), round(raw[2] / s, 4)


def compute_ev_and_kelly(
    model_probs: Tuple[float, float, float],
    odds: Tuple[Optional[float], Optional[float], Optional[float]],
    kelly_fraction: float = 0.25,
    max_stake_pct: float = 5.0,
) -> Dict[str, Any]:
    """
    计算胜/平/负三项的数学期望值 EV = (p * odds) - 1，
    以及 1/4 凯利理性娱乐比例参考（严格封顶 5%，仅作理性参考）。
    """
    labels = [("H", "主胜"), ("D", "平局"), ("A", "客胜")]
    items = []
    best_value = None

    for (code, zh), p, o in zip(labels, model_probs, odds):
        if not o or o <= 1.01:
            items.append({"code": code, "label": zh, "odds": None, "ev_pct": None, "kelly_pct": 0.0, "is_value": False})
            continue
        ev = p * o - 1.0
        b = o - 1.0
        q = 1.0 - p
        full_kelly = (p * b - q) / b if b > 0 else 0.0
        frac_kelly = max(0.0, full_kelly * kelly_fraction) * 100.0
        capped_kelly = round(min(max_stake_pct, frac_kelly), 2)
        ev_pct = round(ev * 100.0, 1)
        is_val = ev_pct >= 3.0

        rec = {
            "code": code,
            "label": zh,
            "odds": round(o, 2),
            "ev_pct": ev_pct,
            "kelly_pct": capped_kelly if is_val else 0.0,
            "is_value": is_val,
        }
        items.append(rec)
        if is_val and (best_value is None or ev_pct > best_value["ev_pct"]):
            best_value = rec

    if best_value:
        note = (
            f"💎 发现正期望价值选项：{best_value['label']}（欧赔 {best_value['odds']}，"
            f"EV {best_value['ev_pct']:+.1f}%，1/4凯利理性参考比例 ≤ {best_value['kelly_pct']}%）"
        )
    elif any(x["odds"] is not None for x in items):
        note = "当前市场赔率已充分定价（三项 EV 均 < +3%），属于合理无明显溢价盘口。"
    else:
        note = "暂无机构欧赔，暂无法测算 EV 价值。"

    return {
        "items": items,
        "best_value": best_value,
        "summary": note,
    }


def invert_market_to_expected_goals(
    mkt_probs: Tuple[float, float, float],
    over_under_line: Optional[float] = None,
) -> Optional[Tuple[float, float]]:
    """
    由市场去水后的胜平负概率 + 大小球盘口线，反推出市场隐含的主客队预期进球 (lam_h, lam_a)。
    专门用于欧冠遇到非五大联赛/冷门联赛球队（无历史数据样本）时的贝叶斯锚定，彻底解决克隆人默认 1580 问题！
    """
    from engine.dixon_coles import build_score_matrix

    mh, md, ma = mkt_probs
    if mh <= 0 or ma <= 0:
        return None

    target_total = float(over_under_line) if (over_under_line and 1.5 <= float(over_under_line) <= 4.5) else 2.65

    def loss(x):
        lh = math.exp(x[0])
        la = math.exp(x[1])
        m = build_score_matrix(lh, la, rho=-0.06, max_goals=7)
        err_p = (m["prob_home"] - mh) ** 2 + (m["prob_draw"] - md) ** 2 + (m["prob_away"] - ma) ** 2
        err_t = 0.03 * ((lh + la - target_total) ** 2)
        return err_p + err_t

    init_h = max(0.35, target_total * (mh + 0.5 * md))
    init_a = max(0.30, target_total * (ma + 0.5 * md))
    try:
        res = minimize(
            loss,
            [math.log(init_h), math.log(init_a)],
            method="L-BFGS-B",
            bounds=[(-1.3, 1.4), (-1.3, 1.4)],
            options={"maxiter": 60},
        )
        lh = round(max(0.25, min(3.8, math.exp(res.x[0]))), 3)
        la = round(max(0.25, min(3.8, math.exp(res.x[1]))), 3)
        return lh, la
    except Exception:
        return None
