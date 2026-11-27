"""
足球比分预测与赛后复盘系统 —— 全局配置中心
第一阶段覆盖：五大联赛（英超/西甲/意甲/德甲/法甲）+ 欧冠
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Any

# 项目根目录与数据目录
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "football_predictor.db"
SETTINGS_PATH = DATA_DIR / "settings.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 覆盖赛事定义（五大联赛 + 欧冠）
LEAGUES: Dict[str, Dict[str, Any]] = {
    "EPL": {
        "name_zh": "英超",
        "name_en": "English Premier League",
        "espn_slug": "eng.1",
        "fd_code": "E0",
        "understat_slug": "EPL",
        "country": "England",
        "default_coords": (51.5074, -0.1278),
    },
    "LALIGA": {
        "name_zh": "西甲",
        "name_en": "Spanish LaLiga",
        "espn_slug": "esp.1",
        "fd_code": "SP1",
        "understat_slug": "La_liga",
        "country": "Spain",
        "default_coords": (40.4168, -3.7038),
    },
    "SERIEA": {
        "name_zh": "意甲",
        "name_en": "Italian Serie A",
        "espn_slug": "ita.1",
        "fd_code": "I1",
        "understat_slug": "Serie_A",
        "country": "Italy",
        "default_coords": (45.4642, 9.1900),
    },
    "BUNDESLIGA": {
        "name_zh": "德甲",
        "name_en": "German Bundesliga",
        "espn_slug": "ger.1",
        "fd_code": "D1",
        "understat_slug": "Bundesliga",
        "country": "Germany",
        "default_coords": (48.1351, 11.5820),
    },
    "LIGUE1": {
        "name_zh": "法甲",
        "name_en": "French Ligue 1",
        "espn_slug": "fra.1",
        "fd_code": "F1",
        "understat_slug": "Ligue_1",
        "country": "France",
        "default_coords": (48.8566, 2.3522),
    },
    "UCL": {
        "name_zh": "欧冠",
        "name_en": "UEFA Champions League",
        "espn_slug": "uefa.champions",
        "fd_code": None,  # 欧冠无单一国内联赛CSV，跨五大联赛球队参数+欧冠赛果融合
        "understat_slug": None,
        "country": "Europe",
        "default_coords": (48.8566, 2.3522),
    },
}

# 当前赛季与上赛季编码（用于 Football-Data.co.uk）
CURRENT_SEASON_FD = "2627"
PREV_SEASON_FD = "2526"
CURRENT_SEASON_UNDERSTAT = "2026"

# ==============================================================================
# 动态情报修正严格封顶规则（单位：百分比小数，例如 0.15 表示对单队预期进球最多修正 ±15%）
# 70% 数理底盘 + 30% 动态情报封顶修正
# ==============================================================================
INTEL_LIMITS: Dict[str, Dict[str, Any]] = {
    "injuries_lineup": {
        "label": "核心伤停 / 临场首发轮换",
        "max_abs": 0.15,
        "desc": "按缺席核心出场时间占比与替补落差打分，轮换替补几乎不扣分",
    },
    "fixture_fatigue": {
        "label": "多线作战与休息时间差",
        "max_abs": 0.10,
        "desc": "高强度赛后72小时恢复周期，重点评估双方休息日差与板凳深度",
    },
    "motivation_context": {
        "label": "球队战意与赛程夹击",
        "max_abs": 0.12,
        "desc": "联赛中段平时为0；仅在争冠/保级生死战、欧冠提前出线/留力夹心赛时触发",
    },
    "coach_morale": {
        "label": "换帅 / 更衣室动荡",
        "max_abs": 0.06,
        "desc": "新帅效应主要集中在前2~3场，更衣室公开矛盾或停训时触发",
    },
    "weather_referee": {
        "label": "极端天气 / 异常裁判触发器",
        "max_abs": 0.05,
        "desc": "平时为0；仅在暴雨/大风/暴雪/酷热或极严厉红点哨撞上高犯规球队时触发",
    },
}

# 单队综合动态修正总封顶（防止叠加失控变成拍脑袋）
TOTAL_INTEL_CAP = 0.25

# 市场赔率红绿灯阈值（咱们的预测概率 vs 市场去水隐含概率的最大绝对差）
MARKET_TRAFFIC_LIGHT = {
    "green_max_diff": 0.08,   # <= 8%：观点基本一致，稳健参考
    "yellow_max_diff": 0.18,  # 8% ~ 18%：存在偏差，若情报支撑充分则提示潜在价值/冷门
    # > 18%：红灯预警，提示可能存在未公开内幕或数据盲区，务必人工复核
}

# 主要球队常见球场坐标（用于 Open-Meteo 精准天气；未命中则自动按联赛默认坐标或城市匹配）
TEAM_STADIUM_COORDS: Dict[str, tuple[float, float]] = {
    # 英超
    "Arsenal": (51.5549, -0.1084),
    "Aston Villa": (52.5091, -1.8848),
    "Bournemouth": (50.7352, -1.8383),
    "Brentford": (51.4907, -0.2886),
    "Brighton": (50.8616, -0.0837),
    "Chelsea": (51.4817, -0.1910),
    "Crystal Palace": (51.3983, -0.0855),
    "Everton": (53.4388, -2.9663),
    "Fulham": (51.4749, -0.2217),
    "Liverpool": (53.4308, -2.9608),
    "Manchester City": (53.4831, -2.2004),
    "Manchester United": (53.4631, -2.2913),
    "Newcastle": (54.9756, -1.6217),
    "Nottingham Forest": (52.9400, -1.1328),
    "Tottenham": (51.6042, -0.0662),
    "West Ham": (51.5387, -0.0166),
    "Wolves": (52.5902, -2.1304),
    "Leicester": (52.6204, -1.1422),
    "Leeds": (53.7779, -1.5722),
    "Sunderland": (54.9144, -1.3882),
    "Burnley": (53.7890, -2.2302),
    # 西甲
    "Real Madrid": (40.4530, -3.6883),
    "Barcelona": (41.3809, 2.1228),
    "Atletico Madrid": (40.4362, -3.5995),
    "Athletic Club": (43.2641, -2.9494),
    "Real Sociedad": (43.3014, -1.9736),
    "Villarreal": (39.9442, -0.1035),
    "Real Betis": (37.3565, -5.9818),
    "Sevilla": (37.3841, -5.9707),
    "Valencia": (39.4746, -0.3582),
    "Girona": (41.9614, 2.8286),
    # 意甲
    "Inter": (45.4781, 9.1240),
    "AC Milan": (45.4781, 9.1240),
    "Juventus": (45.1096, 7.6413),
    "Napoli": (40.8280, 14.1931),
    "AS Roma": (41.9341, 12.4547),
    "Lazio": (41.9341, 12.4547),
    "Atalanta": (45.7089, 9.6808),
    "Fiorentina": (43.7808, 11.2824),
    "Bologna": (44.4923, 11.3098),
    # 德甲
    "Bayern Munich": (48.2188, 11.6247),
    "Borussia Dortmund": (51.4926, 7.4519),
    "Bayer Leverkusen": (51.0383, 7.0022),
    "RB Leipzig": (51.3458, 12.3482),
    "Eintracht Frankfurt": (50.0686, 8.6455),
    "VfB Stuttgart": (48.7923, 9.2320),
    # 法甲
    "Paris Saint-Germain": (48.8414, 2.2530),
    "Marseille": (43.2698, 5.3958),
    "Monaco": (43.7276, 7.4156),
    "Lille": (50.6119, 3.1305),
    "Lyon": (45.7653, 4.9820),
    "Nice": (43.7051, 7.1926),
}

# 球风标签（用于极端天气战术修正：传控流受暴雨大风影响更大）
POSSESSION_TEAMS = {
    "Manchester City", "Arsenal", "Barcelona", "Bayern Munich",
    "Paris Saint-Germain", "Liverpool", "Real Madrid", "Bayer Leverkusen",
    "Brighton", "Tottenham", "Napoli", "Inter", "Chelsea",
}


def load_settings() -> Dict[str, Any]:
    """加载用户自定义设置（如飞书 Webhook、默认关注联赛等）"""
    defaults = {
        "feishu_webhook": "",
        "feishu_secret": "",
        "enabled_leagues": ["EPL", "LALIGA", "SERIEA", "BUNDESLIGA", "LIGUE1", "UCL"],
        "dixon_coles_xi": 0.0065,
        "xg_blend_weight": 0.35,  # 预期进球融合权重（0~1）
        "market_blend_weight": 0.25,  # 市场基准融合参考权重
        "auto_snapshot_hours": [48, 24, 6, 1],
        "auto_push_enabled": True,
        "daily_push_times": ["12:00", "20:00"],
        "review_push_time": "09:30",
        "prematch_1h_enabled": True,
        "model_version": "v2.0",
    }
    if SETTINGS_PATH.exists():
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            defaults.update(saved)
        except Exception:
            pass
    return defaults


def save_settings(new_settings: Dict[str, Any]) -> Dict[str, Any]:
    """保存用户自定义设置"""
    current = load_settings()
    current.update(new_settings)
    SETTINGS_PATH.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return current
