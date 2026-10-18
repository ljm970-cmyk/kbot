"""
================================================================
생존 감시 테스트

  - 정상이면 조용하다
  - 서비스가 꺼졌거나 신호가 5분 넘게 끊기면 알린다
  - 재시작 중 잠깐 끊긴 것은 무시한다 (4분 유예)
  - 계속 죽어 있으면 1시간마다만 다시 알린다
  - 살아나면 복구를 알린다
  - 전송에 실패하면 다음 실행에서 다시 보낸다

실행:  python tests/test_watchdog.py
================================================================
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.heartbeat import heartbeat_path, read_heartbeat, write_heartbeat  # noqa: E402
from ops import watchdog as wd  # noqa: E402

T0 = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)


def _env(d: Path) -> Path:
    p = d / ".env"
    p.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ADMIN_ID=111, 222\n", encoding="utf-8")
    return p


class _Box:
    def __init__(self, active=True, ok=True):
        self.active, self.ok, self.sent = active, ok, []

    def act(self):
        return self.active

    def send(self, token, ids, text):
        self.sent.append((token, ids, text))
        return self.ok


def _setup(beat_at=T0):
    d = Path(tempfile.mkdtemp())
    data = d / "data"
    if beat_at is not None:
        write_heartbeat(data, beat_at)
    return data, _env(d)


def _run(data, env, box, now):
    return wd.run_once(data, env, now=now, active_fn=box.act, send_fn=box.send)


# ================================================================
# 생존 신호 파일
# ================================================================

def test_heartbeat_roundtrip():
    d = Path(tempfile.mkdtemp())
    write_heartbeat(d, T0)
    assert read_heartbeat(d) == T0
    assert heartbeat_path(d).parent.name == "run"


def test_watchdog_reads_bot_heartbeat_format():
    """감시는 봇 코드를 import 하지 않는다. 두 쪽 형식이 같아야 한다."""
    d = Path(tempfile.mkdtemp())
    write_heartbeat(d, T0)
    assert wd.read_heartbeat(d) == T0


def test_missing_heartbeat_reads_none():
    assert wd.read_heartbeat(Path(tempfile.mkdtemp())) is None


# ================================================================
# 판단
# ================================================================

def test_healthy_is_silent():
    data, env = _setup()
    box = _Box()
    assert _run(data, env, box, T0 + timedelta(minutes=1)) is None
    assert box.sent == []


def test_service_down_alerts_after_grace():
    data, env = _setup()
    box = _Box(active=False)
    assert _run(data, env, box, T0) is None                      # 재시작 중일 수 있다
    assert _run(data, env, box, T0 + timedelta(minutes=2)) is None
    msg = _run(data, env, box, T0 + timedelta(minutes=4))
    assert msg and "서비스가 꺼져" in msg
    assert box.sent[0][1] == ["111", "222"]


def test_frozen_loop_detected_by_stale_heartbeat():
    """프로세스는 살아 있어도 신호가 끊기면 멈춘 것"""
    data, env = _setup(beat_at=T0)
    box = _Box(active=True)
    _run(data, env, box, T0 + timedelta(minutes=6))
    msg = _run(data, env, box, T0 + timedelta(minutes=10))
    assert msg and "멈춤 의심" in msg


def test_restart_blip_ignored():
    data, env = _setup()
    box = _Box(active=False)
    _run(data, env, box, T0)
    box.active = True
    write_heartbeat(data, T0 + timedelta(minutes=2))
    assert _run(data, env, box, T0 + timedelta(minutes=2)) is None
    assert box.sent == []


def test_repeat_only_hourly():
    data, env = _setup()
    box = _Box(active=False)
    _run(data, env, box, T0)
    _run(data, env, box, T0 + timedelta(minutes=4))
    for m in range(6, 60, 2):
        _run(data, env, box, T0 + timedelta(minutes=m))
    assert len(box.sent) == 1
    msg = _run(data, env, box, T0 + timedelta(minutes=65))
    assert msg and "(계속)" in msg
    assert len(box.sent) == 2


def test_recovery_message_after_alert():
    data, env = _setup()
    box = _Box(active=False)
    _run(data, env, box, T0)
    _run(data, env, box, T0 + timedelta(minutes=5))
    box.active = True
    write_heartbeat(data, T0 + timedelta(minutes=20))
    msg = _run(data, env, box, T0 + timedelta(minutes=20))
    assert msg and "복구" in msg and "20분" in msg
    assert _run(data, env, box, T0 + timedelta(minutes=22)) is None   # 한 번만


def test_failed_send_retried_next_run():
    data, env = _setup()
    box = _Box(active=False, ok=False)
    _run(data, env, box, T0)
    _run(data, env, box, T0 + timedelta(minutes=5))
    box.ok = True
    assert _run(data, env, box, T0 + timedelta(minutes=7)) is not None
    assert len(box.sent) == 2


def test_failed_recovery_send_retried():
    data, env = _setup()
    box = _Box(active=False)
    _run(data, env, box, T0)
    _run(data, env, box, T0 + timedelta(minutes=5))
    box.active, box.ok = True, False
    write_heartbeat(data, T0 + timedelta(minutes=10))
    _run(data, env, box, T0 + timedelta(minutes=10))
    box.ok = True
    write_heartbeat(data, T0 + timedelta(minutes=12))
    msg = _run(data, env, box, T0 + timedelta(minutes=12))
    assert msg and "복구" in msg


def test_no_env_does_not_crash():
    data, _ = _setup()
    box = _Box(active=False)
    _run(data, Path("/nonexistent/.env"), box, T0)
    _run(data, Path("/nonexistent/.env"), box, T0 + timedelta(minutes=5))
    assert box.sent == []


def test_env_parsing_quotes_and_comments():
    d = Path(tempfile.mkdtemp())
    p = d / ".env"
    p.write_text('# c\nTELEGRAM_BOT_TOKEN="x:y"\n\nTELEGRAM_ADMIN_ID=1\n', encoding="utf-8")
    env = wd.read_env(p)
    assert env["TELEGRAM_BOT_TOKEN"] == "x:y" and env["TELEGRAM_ADMIN_ID"] == "1"


# ================================================================
# 설치·연결
# ================================================================

def test_watchdog_uses_stdlib_only():
    """봇이 import 오류로 못 떠도 감시는 돌아야 한다"""
    src = (ROOT / "ops" / "watchdog.py").read_text(encoding="utf-8")
    for bad in ("from core", "import core", "from scheduler", "telegram.ext", "aiohttp"):
        assert bad not in src


def test_heartbeat_job_is_async():
    """동기 함수면 별도 스레드에서 돌아 이벤트 루프가 멈춰도 신호가 나간다"""
    src = (ROOT / "scheduler" / "engine.py").read_text(encoding="utf-8")
    assert "async def _heartbeat(self)" in src
    assert 'id="heartbeat"' in src


def test_units_and_deploy_hook():
    timer = (ROOT / "ops" / "kbot-watchdog.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=2min" in timer
    svc = (ROOT / "ops" / "kbot-watchdog.service").read_text(encoding="utf-8")
    assert "ops/watchdog.py" in svc and "Type=oneshot" in svc
    deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert "install_watchdog.sh" in deploy


def test_backup_skips_run_dir():
    from core.backup import EXCLUDE_DIRS
    assert "run" in EXCLUDE_DIRS


# ================================================================

if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:
                failed += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print("-" * 60)
    print("전부 통과" if failed == 0 else f"{failed}건 실패")
    sys.exit(1 if failed else 0)
