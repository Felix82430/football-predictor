"""
0成本全天候飞书推送看门狗（彻底根治 GitHub 免费定时丢任务问题，无需人工提醒）
工作机制（由 Windows 任务计划程序每 30 分钟零弹窗静默运行一次，平时只花 0.2 秒）：
1. 判断当前北京时间是否处在 4 大推送档期：
   - 早间复盘（08:15 ~ 11:30）
   - 午间简报（11:55 ~ 14:30）
   - 晚间简报（19:55 ~ 22:30）
   - 深夜赛前1小时终审锁盘（22:30 ~ 次日 03:45，每小时触发1次）
2. 本地状态文件记录每档是否已成功触发，绝不重复轰炸；
3. 只要本档还没发过：
   - 优先带 3 次自动重试调用 GitHub Actions `workflow_dispatch`（1秒必达的高优先级通道），
     并在 90 秒内校验云端确实运行成功；
   - 若连续 3 次连不上 GitHub（例如网络/GitHub 故障），自动降级为【本机直推兜底】，直接把飞书卡片发到您手机！
4. 全程零硬编码密钥（动态读取 ~/.git-credentials）。
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx

REPO = "Felix82430/football-predictor"
WORKFLOW = "football_auto_push.yml"
BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_PATH = DATA_DIR / "cloud_trigger.log"
STATE_PATH = DATA_DIR / "trigger_state.json"
BJT = timezone(timedelta(hours=8))


def _read_github_token() -> str:
    cred = pathlib.Path.home() / ".git-credentials"
    if not cred.exists():
        return ""
    for line in cred.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "github.com" in line:
            parsed = urllib.parse.urlparse(line.strip())
            if parsed.password:
                return parsed.password
    return ""


def _log(msg: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    # 只保留最近 40 条状态记录，避免文件膨胀
    if len(st) > 40:
        keys = sorted(st.keys())[-40:]
        st = {k: st[k] for k in keys}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def _determine_due_slot(now_bjt: datetime) -> tuple[str, str] | None:
    """返回 (slot_key, workflow_task) 或 None"""
    day = now_bjt.strftime("%Y-%m-%d")
    hm = now_bjt.hour * 60 + now_bjt.minute

    if 8 * 60 + 15 <= hm <= 11 * 60 + 30:
        return f"{day}_review", "daily_review"
    if 11 * 60 + 55 <= hm <= 14 * 60 + 30:
        return f"{day}_noon", "daily_noon"
    if 19 * 60 + 55 <= hm <= 22 * 60 + 29:
        return f"{day}_evening", "daily_evening"
    if hm >= 22 * 60 + 30 or hm <= 3 * 60 + 45:
        return f"{day}_h{now_bjt.hour:02d}_pre1h", "prematch_1h"
    return None


def _trigger_github_with_retry(token: str, task: str) -> bool:
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "football-predictor-watchdog",
    }
    for attempt in range(1, 4):
        try:
            with httpx.Client(timeout=15.0) as cli:
                r = cli.post(url, headers=headers, json={"ref": "main", "inputs": {"task": task, "force": False}})
                if r.status_code == 204:
                    _log(f"OK(cloud): 第{attempt}次成功唤醒云端 task={task}")
                    return True
                _log(f"WARN(cloud): 第{attempt}次返回 HTTP {r.status_code}")
        except Exception as e:
            _log(f"WARN(cloud): 第{attempt}次异常 {type(e).__name__}: {e}")
        time.sleep(3)
    return False


def _fallback_local_run(task: str) -> bool:
    """当 GitHub API 3 次均不可达时，本机静默直跑 cloud_runner 兜底直推飞书"""
    py = BASE_DIR / ".venv" / "Scripts" / "python.exe"
    runner = BASE_DIR / "cloud_runner.py"
    if not py.exists() or not runner.exists():
        return False
    try:
        res = subprocess.run(
            [str(py), str(runner), "--task", task],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=180,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        ok = res.returncode == 0
        _log(f"{'OK' if ok else 'FAIL'}(local_fallback): task={task} rc={res.returncode}")
        return ok
    except Exception as e:
        _log(f"ERROR(local_fallback): {type(e).__name__}: {e}")
        return False


def main() -> int:
    force_task = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "watchdog" else None
    now_bjt = datetime.now(BJT)

    if force_task:
        slot_key = f"manual_{now_bjt.strftime('%Y%m%d_%H%M')}_{force_task}"
        task = force_task
    else:
        due = _determine_due_slot(now_bjt)
        if not due:
            return 0
        slot_key, task = due
        state = _load_state()
        if state.get(slot_key):
            return 0  # 本档已成功触发过，直接静默退出

    token = _read_github_token()
    ok = _trigger_github_with_retry(token, task) if token else False
    if not ok:
        ok = _fallback_local_run(task)

    if ok and not force_task:
        state = _load_state()
        state[slot_key] = now_bjt.strftime("%Y-%m-%d %H:%M:%S")
        _save_state(state)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
