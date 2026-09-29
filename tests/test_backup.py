"""
================================================================
장부 백업 테스트

봇 장부(data/)는 VM 디스크에만 있다. 매주 텔레그램 파일로 보내고,
그 파일로 실제로 되살릴 수 있어야 한다.

실행:  python tests/test_backup.py
================================================================
"""

import asyncio
import json
import sqlite3
import sys
import tarfile
import tempfile
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return sys.modules[name]


for mod, attrs in [("aiohttp", {"ClientSession": object,
                                "ClientTimeout": lambda **k: None,
                                "ClientError": Exception}),
                   ("websockets", {"connect": None})]:
    try:
        __import__(mod)
    except ImportError:
        _stub(mod, **attrs)
try:
    import apscheduler  # noqa: F401
except ImportError:
    for p in ["apscheduler", "apscheduler.schedulers", "apscheduler.triggers"]:
        _stub(p)
    _stub("apscheduler.schedulers.asyncio", AsyncIOScheduler=object)
    _stub("apscheduler.triggers.cron", CronTrigger=object)
    _stub("apscheduler.triggers.date", DateTrigger=object)

from core.backup import make_backup  # noqa: E402
from core.order_registry import OrderRegistry  # noqa: E402
from core.state_manager import StateManager  # noqa: E402
from core.t_calculator import FillKind  # noqa: E402
from modes.base_mode import PlannedOrder  # noqa: E402

CFG = {"division": 40, "principal": 20000.0, "fee_rate": 0.0007, "is_active": True}


def _ledger():
    """실제 봇과 같은 구조의 data/ 를 만든다"""
    root = Path(tempfile.mkdtemp())
    data = root / "data"
    sm = StateManager(data)
    sm.save_config("SOXL", CFG)
    st = sm.get_state("SOXL")
    st.T, st.holdings, st.avg_price, st.cash = 3.5, 10, 148.50, 18513.92
    st.last_eod_date = "20260928"
    sm.save_state(st)
    sm.archive_eod("SOXL", "20260928", {"T": 3.5, "holdings": 10})

    reg = OrderRegistry(data / "orders" / "orders.db")
    for i in range(5):
        rid = reg.record_submission("20260928", "SOXL", PlannedOrder(
            tag=FillKind.STAR_BUY, side="buy", trade_type="30", qty=1, price=170.0 + i))
        reg.attach_ord_no(rid, f"00001{i:04d}")

    (data / "backtest").mkdir()
    (data / "backtest" / "SOXL.csv").write_text("date,close\n" * 1000)
    (data / "locks").mkdir(exist_ok=True)
    (data / "locks" / "SOXL.lock").write_text("")
    return root, data, reg


def _names(path):
    with tarfile.open(path) as t:
        return set(t.getnames())


# ================================================================
# 내용
# ================================================================

def test_backup_contains_ledger():
    root, data, _ = _ledger()
    res = make_backup(data, root)
    names = _names(res.path)
    assert "data/state/SOXL.json" in names
    assert "data/config/SOXL.json" in names
    assert "data/orders/orders.db" in names
    assert any(n.startswith("data/archive/") for n in names)
    assert "data/BACKUP_INFO.txt" in names


def test_backup_excludes_cache_locks_and_wal():
    root, data, _ = _ledger()
    (data / "orders" / "orders.db-wal").write_text("x")
    names = _names(make_backup(data, root).path)
    assert not any("backtest" in n for n in names)
    assert not any("locks" in n for n in names)
    assert not any(n.endswith(("-wal", "-shm")) for n in names)


def test_backup_never_contains_env():
    root, data, _ = _ledger()
    (data / ".env").write_text("APP_KEY=secret")
    (root / ".env").write_text("APP_KEY=secret")
    names = _names(make_backup(data, root).path)
    assert not any(n.endswith(".env") for n in names)


def test_summary_shows_ledger():
    root, data, _ = _ledger()
    res = make_backup(data, root)
    assert "SOXL" in res.summary and "3.5000" in res.summary and "10주" in res.summary
    assert "148.50" in res.caption()


# ================================================================
# 복원 왕복
# ================================================================

def test_restore_round_trip():
    """백업 파일만으로 장부와 원장을 그대로 되살릴 수 있어야 한다"""
    root, data, reg = _ledger()
    res = make_backup(data, root)

    restored = Path(tempfile.mkdtemp())
    with tarfile.open(res.path) as t:
        t.extractall(restored)

    sm2 = StateManager(restored / "data")
    st = sm2.get_state("SOXL")
    assert (st.T, st.holdings, st.avg_price, st.cash) == (3.5, 10, 148.50, 18513.92)
    assert st.last_eod_date == "20260928"

    reg2 = OrderRegistry(restored / "data" / "orders" / "orders.db")
    assert len(reg2.day_orders("20260928", "SOXL")) == 5
    assert reg2.by_ord_no("000010003") is not None


def test_db_copy_consistent_while_bot_writes():
    """봇이 원장에 쓰는 도중에 백업해도 사본이 깨지지 않아야 한다"""
    root, data, reg = _ledger()
    stop = threading.Event()

    def writer():
        n = 0
        while not stop.is_set():
            rid = reg.record_submission("20260929", "SOXL", PlannedOrder(
                tag=FillKind.CRASH_BUY, side="buy", trade_type="30", qty=1, price=100.0))
            reg.attach_ord_no(rid, f"W{n}")
            n += 1

    th = threading.Thread(target=writer)
    th.start()
    try:
        results = [make_backup(data, root) for _ in range(3)]
    finally:
        stop.set()
        th.join()

    for res in results:
        out = Path(tempfile.mkdtemp())
        with tarfile.open(res.path) as t:
            t.extractall(out)
        db = sqlite3.connect(out / "data" / "orders" / "orders.db")
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        db.close()


# ================================================================
# 스케줄러 · 텔레그램
# ================================================================

def _engine(data, notifier):
    from scheduler.engine import SchedulerEngine
    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr = StateManager(data)
    eng.notifier = notifier
    return eng


def test_send_backup_delivers_file_and_cleans_up():
    root, data, _ = _ledger()
    got = {}

    class N:
        async def send(self, text): got.setdefault("text", []).append(text)

        async def send_document(self, path, caption=""):
            got["size"] = Path(path).stat().st_size
            got["path"] = Path(path)
            got["caption"] = caption
            return True

    ok = asyncio.run(_engine(data, N()).send_backup(reason="요청"))
    assert ok is True
    assert got["size"] > 0
    assert got["caption"].startswith("[요청]") and "SOXL" in got["caption"]
    assert not got["path"].exists(), "보낸 뒤 임시 파일이 남아 있다"


def test_send_backup_without_file_channel():
    root, data, _ = _ledger()

    class N:
        async def send(self, text): pass

    assert asyncio.run(_engine(data, N()).send_backup()) is False


def test_weekly_backup_scheduled_after_friday_eod():
    src = (ROOT / "scheduler" / "engine.py").read_text(encoding="utf-8")
    assert 'BACKUP_CRON = {"day_of_week": "sat", "hour": 9, "minute": 10}' in src
    assert 'id="weekly_backup"' in src


def test_backup_command_registered():
    src = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    assert 'CommandHandler("backup"' in src
    assert "async def send_document" in src
    assert "/backup" in src[src.index("async def _cmd_help"):]


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
