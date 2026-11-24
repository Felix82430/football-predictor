"""
Open-Meteo 永久免费气象接口客户端（无需 API Key）
执行咱们约定的【极端天气 × 战术风格触发器】：
- 平时正常天气修正严格为 0，绝不制造伪科学噪音；
- 仅在暴雨/大风/暴雪/严寒/酷热时触发，对传控流进攻打折或下调总预期进球。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Tuple

import httpx

from config import LEAGUES, POSSESSION_TEAMS, TEAM_STADIUM_COORDS

WMO_WEATHER_ZH = {
    0: "晴朗",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴天",
    45: "有雾",
    48: "冻雾",
    51: "小毛毛雨",
    53: "中毛毛雨",
    55: "浓毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "阵雨",
    81: "中阵雨",
    82: "暴雨",
    95: "雷暴",
    96: "雷暴伴冰雹",
    99: "强雷暴伴冰雹",
}


def get_stadium_coords(home_team: str, league_code: str) -> Tuple[float, float]:
    if home_team in TEAM_STADIUM_COORDS:
        return TEAM_STADIUM_COORDS[home_team]
    return LEAGUES.get(league_code, {}).get("default_coords", (48.8566, 2.3522))


def fetch_match_weather(
    home_team: str,
    away_team: str,
    league_code: str,
    kickoff_utc: str,
    timeout: float = 8.0,
) -> Dict[str, Any]:
    """查询球场在开赛时刻的天气，并评估是否触发极端天气战术修正"""
    lat, lon = get_stadium_coords(home_team, league_code)
    try:
        dt = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
        target_hour = dt.strftime("%Y-%m-%dT%H:00")
        date_str = dt.strftime("%Y-%m-%d")
    except Exception:
        return {"triggered": False, "summary": "开球时间未知，未触发天气修正", "home_delta": 0.0, "away_delta": 0.0}

    # 仅预测 14 天以内的天气
    now_utc = datetime.now(timezone.utc)
    diff_days = (dt - now_utc).days
    if diff_days > 14 or diff_days < -5:
        return {"triggered": False, "summary": "超出天气预报窗口，修正为0", "home_delta": 0.0, "away_delta": 0.0}

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,precipitation,wind_speed_10m,weather_code"
        f"&start_date={date_str}&end_date={date_str}&timezone=UTC"
    )
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return {"triggered": False, "summary": "气象接口暂未返回，修正为0", "home_delta": 0.0, "away_delta": 0.0}

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    idx = times.index(target_hour) if target_hour in times else (len(times) // 2 if times else -1)
    if idx < 0:
        return {"triggered": False, "summary": "无对应小时气象，修正为0", "home_delta": 0.0, "away_delta": 0.0}

    temp = float((hourly.get("temperature_2m") or [15.0])[idx])
    rain = float((hourly.get("precipitation") or [0.0])[idx])
    wind = float((hourly.get("wind_speed_10m") or [10.0])[idx])
    code = int((hourly.get("weather_code") or [0])[idx])
    desc = WMO_WEATHER_ZH.get(code, f"天气代码{code}")

    triggered = False
    reasons = []
    home_delta = 0.0
    away_delta = 0.0

    # 极端降雨/大雪
    if rain >= 4.0 or code in (65, 73, 75, 82, 95, 96, 99):
        triggered = True
        reasons.append(f"强降水/恶劣天气（{desc}，降水量{rain:.1f}mm/h），地面传控与射门精度受损")
        home_delta -= 0.04 if home_team in POSSESSION_TEAMS else 0.02
        away_delta -= 0.04 if away_team in POSSESSION_TEAMS else 0.02

    # 强风（>35 km/h）
    if wind >= 35.0:
        triggered = True
        reasons.append(f"大风预警（风速{wind:.1f}km/h），高空球与长传精准度下降")
        home_delta -= 0.02
        away_delta -= 0.02

    # 极端高温（>=30°C）或严寒（<=0°C）
    if temp >= 30.0:
        triggered = True
        reasons.append(f"酷热天气（{temp:.1f}°C），下半场体能消耗加倍、节奏趋慢")
        home_delta -= 0.02
        away_delta -= 0.02
    elif temp <= 0.0:
        triggered = True
        reasons.append(f"严寒冰冻（{temp:.1f}°C），场地偏硬影响技术流发挥")
        home_delta -= 0.015
        away_delta -= 0.015

    # 严格遵守 ±5% 封顶
    home_delta = round(max(-0.05, min(0.05, home_delta)), 4)
    away_delta = round(max(-0.05, min(0.05, away_delta)), 4)

    summary = (
        f"{desc} | {temp:.1f}°C | 降水{rain:.1f}mm/h | 风速{wind:.1f}km/h"
        + (f" 【触发极端天气修正：{'；'.join(reasons)}】" if triggered else "（正常天气，修正为0）")
    )
    return {
        "triggered": triggered,
        "temp_c": temp,
        "rain_mm": rain,
        "wind_kmh": wind,
        "condition": desc,
        "home_delta": home_delta,
        "away_delta": away_delta,
        "reasons": reasons,
        "summary": summary,
    }
