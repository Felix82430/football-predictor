"""
专业 Dixon-Coles (1997) 足球比分概率模型（含时间衰减 + 低比分相关性修正）
参考开源项目 martineastwood/penaltyblog 算法标准，采用纯 NumPy + SciPy 向量化
L-BFGS-B 最大似然估计，无需 C++ 编译环境，拟合单个联赛耗时 < 0.4 秒。

核心数学优势：
1. 解决传统泊松分布对 0-0 / 1-0 / 0-1 / 1-1 低比分估计失准的问题（引入 rho 修正系数）
2. 指数时间衰减权重 exp(-xi * days_ago)，越近的比赛话语权越大
3. 支持把动态情报修正后的预期进球直接重新投影成精准的 8x8 比分矩阵与 Top 3 比分组合
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson


def dixon_coles_tau(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    """Dixon-Coles 低比分修正因子 tau(x, y)"""
    if x == 0 and y == 0:
        return max(1e-6, 1.0 - lam * mu * rho)
    if x == 0 and y == 1:
        return max(1e-6, 1.0 + lam * rho)
    if x == 1 and y == 0:
        return max(1e-6, 1.0 + mu * rho)
    if x == 1 and y == 1:
        return max(1e-6, 1.0 - rho)
    return 1.0


def build_score_matrix(
    lam_home: float,
    lam_away: float,
    rho: float = -0.06,
    max_goals: int = 8,
) -> Dict[str, Any]:
    """
    给定主客队预期进球（lam_home, lam_away）与低比分相关系数 rho，
    构建完整比分概率矩阵，输出胜平负概率、Top 5 比分、大小球(2.5)、双方进球(BTTS)。
    """
    lam_h = max(0.15, min(5.0, float(lam_home)))
    lam_a = max(0.15, min(5.0, float(lam_away)))

    h_probs = poisson.pmf(np.arange(max_goals + 1), lam_h)
    a_probs = poisson.pmf(np.arange(max_goals + 1), lam_a)
    mat = np.outer(h_probs, a_probs)

    # 应用 Dixon-Coles 四个低比分格修正
    for i in (0, 1):
        for j in (0, 1):
            mat[i, j] *= dixon_coles_tau(i, j, lam_h, lam_a, rho)

    mat = np.clip(mat, 0.0, None)
    total_mass = float(mat.sum())
    if total_mass > 0:
        mat /= total_mass

    prob_home = float(np.tril(mat, k=-1).sum())
    prob_draw = float(np.trace(mat))
    prob_away = float(np.triu(mat, k=1).sum())

    # 大小球（>2.5球）与双方均进球（BTTS）
    over_25 = 0.0
    btts = 0.0
    scores_flat: List[Tuple[str, float]] = []
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            p = float(mat[i, j])
            scores_flat.append((f"{i}-{j}", p))
            if i + j >= 3:
                over_25 += p
            if i >= 1 and j >= 1:
                btts += p

    scores_flat.sort(key=lambda x: x[1], reverse=True)
    top_scores = [{"score": sc, "prob": round(pr, 4)} for sc, pr in scores_flat[:5]]

    # 确定主推胜平负方向
    probs_map = {"H": prob_home, "D": prob_draw, "A": prob_away}
    recommended = max(probs_map, key=probs_map.get)

    return {
        "lam_home": round(lam_h, 3),
        "lam_away": round(lam_a, 3),
        "prob_home": round(prob_home, 4),
        "prob_draw": round(prob_draw, 4),
        "prob_away": round(prob_away, 4),
        "recommended_outcome": recommended,
        "top3_scores": top_scores[:3],
        "top5_scores": top_scores,
        "over_25_prob": round(over_25, 4),
        "btts_prob": round(btts, 4),
    }


class DixonColesModel:
    """时间衰减 Dixon-Coles 拟合与预测类"""

    def __init__(self, xi: float = 0.0065, l2_reg: float = 0.002):
        self.xi = xi
        self.l2_reg = l2_reg
        self.teams: List[str] = []
        self.team_to_idx: Dict[str, int] = {}
        self.attack: Dict[str, float] = {}
        self.defense: Dict[str, float] = {}
        self.home_adv: float = 0.25
        self.rho: float = -0.06
        self.is_fitted: bool = False

    def fit(self, df: pd.DataFrame, ref_date: Optional[pd.Timestamp] = None) -> "DixonColesModel":
        """拟合联赛历史数据（Date, HomeTeam, AwayTeam, FTHG, FTAG）"""
        if df.empty or len(df) < 20:
            self.is_fitted = False
            return self

        work = df.dropna(subset=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]).copy()
        work["Date"] = pd.to_datetime(work["Date"])
        if ref_date is None:
            ref_date = work["Date"].max()
        else:
            work = work[work["Date"] < ref_date]

        if len(work) < 20:
            self.is_fitted = False
            return self

        teams = sorted(set(work["HomeTeam"]).union(set(work["AwayTeam"])))
        n_teams = len(teams)
        team_to_idx = {t: i for i, t in enumerate(teams)}

        h_idx = work["HomeTeam"].map(team_to_idx).to_numpy(dtype=np.int64)
        a_idx = work["AwayTeam"].map(team_to_idx).to_numpy(dtype=np.int64)
        hg = work["FTHG"].to_numpy(dtype=np.int64)
        ag = work["FTAG"].to_numpy(dtype=np.int64)

        days_ago = (ref_date - work["Date"]).dt.days.clip(lower=0).to_numpy(dtype=np.float64)
        weights = np.exp(-self.xi * days_ago)

        # 参数向量：[attack (n_teams), defense (n_teams), home_adv (1), rho (1)]
        init_params = np.zeros(2 * n_teams + 2, dtype=np.float64)
        init_params[-2] = 0.25
        init_params[-1] = -0.05

        # 常数项 log(k!) 不影响优化极值，省去重复计算提升速度
        def neg_log_likelihood(params: np.ndarray) -> float:
            atk = params[:n_teams]
            dfd = params[n_teams : 2 * n_teams]
            # 中心化攻防参数防多重共线性漂移
            atk_c = atk - np.mean(atk)
            dfd_c = dfd - np.mean(dfd)
            h_adv = params[-2]
            rho = params[-1]

            log_lam = np.clip(atk_c[h_idx] + dfd_c[a_idx] + h_adv, -3.5, 2.5)
            log_mu = np.clip(atk_c[a_idx] + dfd_c[h_idx], -3.5, 2.5)
            lam = np.exp(log_lam)
            mu = np.exp(log_mu)

            # 泊松对数似然（略去 log(k!) 常数）
            ll = hg * log_lam - lam + ag * log_mu - mu

            # 向量化 Dixon-Coles tau 修正
            tau = np.ones_like(lam)
            m00 = (hg == 0) & (ag == 0)
            m01 = (hg == 0) & (ag == 1)
            m10 = (hg == 1) & (ag == 0)
            m11 = (hg == 1) & (ag == 1)
            tau[m00] = 1.0 - lam[m00] * mu[m00] * rho
            tau[m01] = 1.0 + lam[m01] * rho
            tau[m10] = 1.0 + mu[m10] * rho
            tau[m11] = 1.0 - rho

            if np.any(tau <= 1e-6):
                return 1e9

            ll += np.log(tau)
            reg = self.l2_reg * (np.sum(atk_c ** 2) + np.sum(dfd_c ** 2))
            return float(-np.sum(weights * ll) + reg)

        bounds = [(-2.0, 2.0)] * (2 * n_teams) + [(-0.5, 1.0), (-0.25, 0.25)]
        res = minimize(
            neg_log_likelihood,
            init_params,
            method="L-BFGS-B",
            bounds=bounds,
            options={"maxiter": 180, "ftol": 1e-6},
        )

        params = res.x
        atk = params[:n_teams] - np.mean(params[:n_teams])
        dfd = params[n_teams : 2 * n_teams] - np.mean(params[n_teams : 2 * n_teams])

        self.teams = teams
        self.team_to_idx = team_to_idx
        self.attack = {t: float(atk[i]) for i, t in enumerate(teams)}
        self.defense = {t: float(dfd[i]) for i, t in enumerate(teams)}
        self.home_adv = float(params[-2])
        self.rho = float(params[-1])
        self.is_fitted = True
        return self

    def predict_expected_goals(self, home_team: str, away_team: str) -> Optional[Tuple[float, float]]:
        if not self.is_fitted:
            return None
        if home_team not in self.attack or away_team not in self.attack:
            return None
        lam_h = math.exp(self.attack[home_team] + self.defense[away_team] + self.home_adv)
        lam_a = math.exp(self.attack[away_team] + self.defense[home_team])
        return round(lam_h, 3), round(lam_a, 3)

    def predict(self, home_team: str, away_team: str) -> Optional[Dict[str, Any]]:
        eg = self.predict_expected_goals(home_team, away_team)
        if eg is None:
            return None
        return build_score_matrix(eg[0], eg[1], rho=self.rho)
