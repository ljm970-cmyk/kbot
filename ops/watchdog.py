#!/usr/bin/env python3
"""
================================================================
봇 생존 감시 — 봇과 따로 돈다

봇이 죽거나 멈추면 봇이 직접 알릴 수 없다. systemd 타이머가 2분마다
이 스크립트를 실행해서 확인하고, 문제가 있으면 텔레그램으로 바로 알린다.

  확인 1  systemctl is-active kbot      서비스가 떠 있는가
  확인 2  data/run/heartbeat.json       최근 5분 안에 신호가 있었는가
                                        (프로세스는 있는데 멈춘 경우)

  - 배포·재시작 중 잠깐 끊기는 것은 무시한다 (4분 이상 이어져야 알림)
  - 계속 죽어 있으면 1시간마다 다시 알린다
  - 살아나면 "복구" 를 알린다

봇 코드에 의존하지 않는다 (표준 라이브러리만). 봇이 import 오류로
못 뜨는 상황에서도 이 감시는 돌아야 하기 때문이다.

VM 자체가 꺼지면 이 감시도 같이 꺼진다. 그래서 .env 에 HEALTHCHECK_URL
(healthchecks.io 등)을 넣으면, 봇이 정상일 때마다 그 주소로 "살아있음" 을
보낸다. 신호가 끊기면 외부 서비스가 텔레그램·메일로 알린다 — VM 이 꺼지거나
네트워크가 끊겨도 잡힌다. 봇에 문제가 생겨 이쪽이 알림을 보낼 때는 /fail 로
알려 외부 쪽도 바로 빨간불이 된다.

수동 실행
  venv/bin/python ops/watchdog.py           한 번 확인
  venv/bin/python ops/watchdog.py --test    텔레그램 시험 메시지
================================================================
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ.get("KBOT_HOME", Path(__file__).resolve().parent.parent))
ENV_FILE = Path(os.environ.get("KBOT_ENV_FILE", HOME / ".env"))


def _data_dir() -> Path:
    """봇과 같은 곳을 본다 — 환경변수, 없으면 .env, 없으면 기본값"""
    v = os.environ.get("KBOT_DATA_DIR") or read_env(ENV_FILE).get("KBOT_DATA_DIR")
    return Path(v) if v else HOME / "data"
SERVICE = os.environ.get("KBOT_SERVICE", "kbot")

STALE_AFTER = timedelta(minutes=5)      # 이보다 오래 신호가 없으면 멈춘 것
GRACE = timedelta(minutes=4)            # 이만큼 이어져야 알린다 (재시작 무시)
REPEAT = timedelta(hours=1)             # 죽어 있는 동안 다시 알리는 간격

KST = timezone(timedelta(hours=9))


# ----------------------------------------------------------------
# 입출력 (테스트에서 바꿔 끼운다)
# ----------------------------------------------------------------

def read_env(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def service_active(name: str = SERVICE) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", name], timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def read_heartbeat(data_dir: Path) -> datetime | None:
    try:
        raw = json.loads((data_dir / "run" / "heartbeat.json").read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(raw["ts"])
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def ping(url: str, fail: bool = False, body: str = "") -> bool:
    """외부 감시(healthchecks.io 형식)에 신호. 정상은 url, 문제는 url/fail."""
    if not url:
        return False
    target = url.rstrip("/") + ("/fail" if fail else "")
    data = body.encode("utf-8")[:10000] if body else None
    try:
        req = urllib.request.Request(target, data=data, method="POST" if data else "GET")
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"외부 감시 신호 실패: {e}", file=sys.stderr)
        return False


def send_telegram(token: str, chat_ids: list[str], text: str) -> bool:
    ok = False
    for cid in chat_ids:
        data = urllib.parse.urlencode({"chat_id": cid, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                ok = ok or r.status == 200
        except Exception as e:
            print(f"텔레그램 전송 실패: {e}", file=sys.stderr)
    return ok


# ----------------------------------------------------------------
# 판단
# ----------------------------------------------------------------

def diagnose(active: bool, beat: datetime | None, now: datetime) -> str | None:
    """문제가 있으면 사유, 없으면 None"""
    if not active:
        return "서비스가 꺼져 있습니다"
    if beat is None:
        return "생존 신호 파일이 없습니다"
    age = now - beat
    if age > STALE_AFTER:
        return f"생존 신호가 {int(age.total_seconds() // 60)}분째 없습니다 (멈춤 의심)"
    return None


def _fmt(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%m/%d %H:%M")


def decide(problem: str | None, state: dict, now: datetime) -> tuple[str | None, dict]:
    """(보낼 메시지 또는 None, 새 상태)"""
    state = dict(state)
    if problem is None:
        msg = None
        if state.get("alerted"):
            since = datetime.fromisoformat(state["down_since"])
            mins = int((now - since).total_seconds() // 60)
            msg = (f"✅ kbot 복구 — 다시 정상입니다\n"
                   f"  멈춘 시각 {_fmt(since)} · 약 {mins}분\n"
                   f"  그 사이 주문 시간이 있었다면 /orders 로 확인하세요.")
        return msg, {}

    if not state.get("down_since"):
        state["down_since"] = now.isoformat(timespec="seconds")
    since = datetime.fromisoformat(state["down_since"])
    if now - since < GRACE:
        return None, state                       # 재시작 중일 수 있다

    last = state.get("last_alert")
    if last and now - datetime.fromisoformat(last) < REPEAT:
        return None, state

    again = " (계속)" if state.get("alerted") else ""
    msg = (f"🚨 kbot 응답 없음{again}\n"
           f"  {problem}\n"
           f"  시작 {_fmt(since)} (KST)\n\n"
           f"VM 에서 확인:\n"
           f"  sudo systemctl status {SERVICE}\n"
           f"  sudo journalctl -u {SERVICE} -n 50 --no-pager\n"
           f"다시 띄우기:\n"
           f"  sudo systemctl restart {SERVICE}")
    state["alerted"] = True
    state["last_alert"] = now.isoformat(timespec="seconds")
    state["problem"] = problem
    return msg, state


# ----------------------------------------------------------------

def _state_path(data_dir: Path) -> Path:
    return data_dir / "run" / "watchdog.json"


def _load_state(data_dir: Path) -> dict:
    try:
        return json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(data_dir: Path, state: dict) -> None:
    p = _state_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def run_once(data_dir: Path | None = None, env_file: Path = ENV_FILE, now: datetime | None = None,
             active_fn=service_active, send_fn=send_telegram, ping_fn=ping) -> str | None:
    data_dir = data_dir or _data_dir()
    now = now or datetime.now(timezone.utc)
    problem = diagnose(active_fn(), read_heartbeat(data_dir), now)
    old = _load_state(data_dir)
    msg, state = decide(problem, old, now)
    if msg:
        env = read_env(env_file)
        token = env.get("TELEGRAM_BOT_TOKEN", "")
        ids = [x.strip() for x in env.get("TELEGRAM_ADMIN_ID", "").split(",") if x.strip()]
        sent = bool(token and ids) and send_fn(token, ids, msg)
        if not (token and ids):
            print("텔레그램 설정을 .env 에서 찾지 못했습니다", file=sys.stderr)
        if not sent:
            # 못 보냈으면 다음 실행에서 다시 보내도록 기록을 되돌린다
            state = old if problem is None else {**state, "last_alert": None,
                                                 "alerted": old.get("alerted", False)}
    _save_state(data_dir, state)

    # 외부 감시 — 정상이면 살아있음, 알림을 낸 순간이면 실패 신호.
    # 재시작 유예 중에는 보내지 않는다 (외부 쪽 유예 시간이 덮는다).
    url = read_env(env_file).get("HEALTHCHECK_URL", "").strip()
    if url:
        if problem is None:
            ping_fn(url)
        elif msg:
            ping_fn(url, fail=True, body=problem)
    return msg


def main(argv: list[str]) -> int:
    if "--test" in argv:
        env = read_env(ENV_FILE)
        ids = [x.strip() for x in env.get("TELEGRAM_ADMIN_ID", "").split(",") if x.strip()]
        ok = send_telegram(env.get("TELEGRAM_BOT_TOKEN", ""), ids,
                           "🧪 kbot 감시 시험 메시지 — 이게 보이면 알림 경로는 정상입니다.")
        print("텔레그램: " + ("보냄" if ok else "실패"))
        url = env.get("HEALTHCHECK_URL", "").strip()
        if url:
            hc = ping(url)
            print("외부 감시: " + ("신호 보냄 — healthchecks 화면에서 초록불 확인" if hc else "실패"))
            ok = ok and hc
        else:
            print("외부 감시: HEALTHCHECK_URL 없음 (설정 안 함)")
        return 0 if ok else 1
    msg = run_once()
    now = datetime.now(timezone.utc)
    beat = read_heartbeat(_data_dir())
    print(f"[{_fmt(now)}] 서비스 {'on' if service_active() else 'off'} · "
          f"신호 {_fmt(beat) if beat else '없음'}" + (f" · 알림: {msg.splitlines()[0]}" if msg else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
