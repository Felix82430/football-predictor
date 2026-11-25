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
from scipy.stats import poisson


def implied_total_goals_from_ou(
    line: Optional[float],
    over_odds: Optional[float] = None,
    under_odds: Optional[float] = None,
) -> float:
    """
    v1.6 核心修复：结合大小球盘口线 + 大/小球双边赔率水位，
    通过泊松累积分布精确反推市场真实的期望总进球数。
    彻底解决之前把 overUnder=3.5（即使大球赔率高达2.6）直接当成 3.5 球拉爆预期进球的 Bug！
    """
    if not line or not (1.5 <= float(line) <= 5.5):
        return 2.58

    L = float(line)
    # 若有双边赔率，先去水求真实大球概率 P(Total > L)
    if over_odds and under_odds and over_odds > 1.01 and under_odds > 1.01:
        inv_o, inv_u = 1.0 / over_odds, 1.0 / under_odds
        p_over = inv_o / (inv_o + inv_u)
        # 整数/半数盘口换算泊松阈值 k（例如 L=2.5 -> 进>=3球即 k=2；L=3.5 -> 进>=4球即 k=3）
        k = int(math.floor(L))
        # 对半数盘口直接使用 Poisson(Total > k) = 1 - cdf(k, lam) = p_over
        try:
            def f(lam: float) -> float:
                return (1.0 - float(poisson.cdf(k, lam))) - p_over

            lam_star = brentq(f, 0.8, 6.0, xtol=1e-4)
            # 合理温和限幅，防止极端大球线偏离现实
            return round(max(1.75, min(3.75, lam_star)), 3)
        except Exception:
            pass

    # 若暂缺双边水位，对偏高盘口线做保守回归（避免 3.5 线被直接当成 3.5 球）
    return round(max(1.85, min(3.25, 0.45 * L + 0.55 * 2.58)), 3)


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
            f"💎 发现高性价比选项：{best_value['label']}（参考赔率 {best_value['odds']}，"
            f"相比胜率赔率偏高 {best_value['ev_pct']:+.1f}%，如娱乐建议轻仓 ≤ {best_value['kelly_pct']}%）"
        )
    elif any(x["odds"] is not None for x in items):
        note = "当前各选项赔率定价比较合理，没有明显捡漏空间，按胜率与防冷思路参考即可。"
    else:
        note = "暂无公开赔率数据，仅按两队硬实力与近况分析。"

    return {
        "items": items,
        "best_value": best_value,
        "summary": note,
    }


def invert_market_to_expected_goals(
    mkt_probs: Tuple[float, float, float],
    over_under_line: Optional[float] = None,
    over_odds: Optional[float] = None,
    under_odds: Optional[float] = None,
) -> Optional[Tuple[float, float]]:
    """
    由市场去水后的胜平负概率 + 结合水位的真实总进球期望，反推出主客队预期进球 (lam_h, lam_a)。
    """
    from engine.dixon_coles import build_score_matrix

    mh, md, ma = mkt_probs
    if mh <= 0 or ma <= 0:
        return None

    target_total = implied_total_goals_from_ou(over_under_line, over_odds, under_odds)

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
