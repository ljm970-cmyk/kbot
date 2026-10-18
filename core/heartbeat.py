"""
================================================================
생존 신호 (heartbeat)

봇은 1분마다 data/run/heartbeat.json 에 시각을 적는다. 스케줄러
이벤트 루프 안에서 적으므로, 프로세스는 살아 있는데 루프가 멈춘
경우(무한 대기, 교착)도 신호가 끊긴다.

봇과 별개로 도는 감시(ops/watchdog.py)가 이 파일을 읽는다.
================================================================
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

RUN_DIR = "run"
HEARTBEAT_FILE = "heartbeat.json"


def heartbeat_path(data_dir: Path) -> Path:
    return Path(data_dir) / RUN_DIR / HEARTBEAT_FILE


def write_heartbeat(data_dir: Path, now: datetime | None = None) -> Path:
    path = heartbeat_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": now.isoformat(timespec="seconds"),
                               "pid": os.getpid()}), encoding="utf-8")
    tmp.replace(path)
    return path


def read_heartbeat(data_dir: Path) -> datetime | None:
    try:
        raw = json.loads(heartbeat_path(data_dir).read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(raw["ts"])
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None
