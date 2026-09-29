"""
================================================================
장부 백업

봇 장부(data/)는 VM 디스크에만 있다. 코드는 GitHub 에 남지만 T값·평단·
주문 원장은 VM 이 날아가면 되살릴 수 없다. 주기적으로 압축해서 텔레그램
파일로 보낸다. (저장소가 공개라 GitHub 에는 올리지 않는다.)

담는 것
  state/    종목별 T값·평단·보유·잔금
  config/   종목별 분할·원금·수수료
  orders/   주문 원장 (태그 ↔ 주문번호)
  fills/    실시간 체결 수신 기록
  archive/  일별 정산 기록, 보정 이력

빼는 것
  backtest/ 가격 캐시 (다시 받으면 된다)
  locks/    잠금 파일
  .env      키 — 백업에 절대 넣지 않는다 (data/ 밖에 있다)

SQLite 원장은 봇이 쓰는 도중에 파일을 그대로 복사하면 깨질 수 있다.
SQLite 온라인 백업 기능으로 일관된 사본을 뜬다.

복원
  sudo systemctl stop kbot
  mv ~/kbot/data ~/kbot/data.before-restore
  tar -xzf kbot-backup-*.tar.gz -C ~/kbot
  sudo systemctl start kbot
================================================================
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

#: data/ 아래에서 백업하지 않는 최상위 폴더
EXCLUDE_DIRS = frozenset({"backtest", "locks"})
#: SQLite 보조 파일 — 온라인 백업 사본에 이미 반영된다
SKIP_SUFFIXES = ("-wal", "-shm", "-journal")


@dataclass
class BackupResult:
    path: Path
    files: int
    size: int
    summary: str

    def caption(self) -> str:
        kb = self.size / 1024
        return (f"💾 장부 백업 — {self.path.name}\n"
                f"  파일 {self.files}개 · {kb:,.0f}KB\n\n"
                f"{self.summary}\n\n"
                f"복원 방법은 README 의 '장부 백업' 을 보세요.")


def _copy_sqlite(src: Path, dst: Path) -> None:
    """쓰는 중인 DB 도 깨지지 않게 온라인 백업으로 복사한다"""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _ledger_summary(data_dir: Path) -> str:
    """백업 시점 장부 요약 — 받은 파일이 맞는 시점인지 바로 볼 수 있게"""
    lines = []
    for p in sorted((data_dir / "state").glob("*.json")):
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            lines.append(f"  {p.stem}: 읽기 실패")
            continue
        halted = " [정지]" if st.get("halted") else ""
        lines.append(
            f"  {p.stem}{halted} T {float(st.get('T', 0)):.4f} · "
            f"{int(st.get('holdings', 0))}주 @${float(st.get('avg_price', 0)):.2f} · "
            f"잔금 ${float(st.get('cash', 0)):,.2f} · 정산 {st.get('last_eod_date') or '-'}")
    return "\n".join(lines) if lines else "  종목 상태 파일 없음"


def make_backup(data_dir: Path, out_dir: Path | None = None) -> BackupResult:
    """data/ 를 tar.gz 로 묶는다. 결과 파일은 호출한 쪽이 지운다."""
    data_dir = Path(data_dir)
    out_dir = Path(out_dir or tempfile.gettempdir())
    stamp = datetime.now(KST).strftime("%Y%m%d-%H%M")
    path = out_dir / f"kbot-backup-{stamp}.tar.gz"

    count = 0
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "data"
        stage.mkdir()
        for src in sorted(data_dir.rglob("*")):
            rel = src.relative_to(data_dir)
            if not src.is_file() or (rel.parts and rel.parts[0] in EXCLUDE_DIRS):
                continue
            if src.name.endswith(SKIP_SUFFIXES) or src.name == ".env":
                continue
            dst = stage / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.suffix == ".db":
                _copy_sqlite(src, dst)
            else:
                shutil.copy2(src, dst)
            count += 1

        summary = _ledger_summary(data_dir)
        (stage / "BACKUP_INFO.txt").write_text(
            f"kbot 장부 백업\n생성 {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST\n"
            f"파일 {count}개\n\n{summary}\n", encoding="utf-8")

        with tarfile.open(path, "w:gz") as tar:
            tar.add(stage, arcname="data")

    return BackupResult(path=path, files=count, size=path.stat().st_size, summary=summary)
