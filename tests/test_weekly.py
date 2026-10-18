"""
================================================================
주간 성과 요약

실행:  python tests/test_weekly.py
================================================================
"""

import asyncio
import sys
import tempfile
import types
from datetime import date
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

from core.funding import (  # noqa: E402
    FundingCheck,
    allocate_buys,
    core_buy_need,
    order_cost,
)
from core.order_registry import OrderRegistry  # noqa: E402
from core.state_manager import StateManager  # noqa: E402
from core.t_calculator import FillKind  # noqa: E402
from kiwoom.constants import TradeType  # noqa: E402
from modes.base_mode import MarketSnapshot, PlannedOrder, PositionState  # noqa: E402
from modes.normal_mode import NormalMode  # noqa: E402

from core.weekly import week_range, weekly_report  # noqa: E402

SAT = date(2026, 10, 24)


def _row(d, T, h, avg, cash, close, pl=0.0, closed=False, mode="normal"):
    return {"trade_date": d, "T": T, "holdings": h, "avg_price": avg, "cash": cash,
            "close": close, "realized_pl": pl, "cycle_closed": closed, "mode": mode}


def _setup(tickers=("SOXL",), rows=None):
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    reg = OrderRegistry(d / "o.db")
    for t in tickers:
        sm.save_config(t, {"ticker": t, "division": 40, "principal": 20000.0,
                           "fee_rate": 0.0007, "is_active": True})
    for t, r in (rows or []):
        sm.archive_eod(t, r["trade_date"], r)
    return sm, reg


def _fill(reg, t, day, side, qty, price, tag):
    from modes.base_mode import PlannedOrder
    o = PlannedOrder(tag=tag, side=side, trade_type=TradeType.LOC, qty=qty, price=price)
    rid = reg.record_submission(day, t, o)
    reg.attach_ord_no(rid, f"{t}{day}{tag}{qty}")
    with reg._conn() as c:
        c.execute("UPDATE orders SET status='filled', filled_qty=?, filled_price=? WHERE id=?",
                  (qty, price, rid))


SOXL_ROWS = [
    ("SOXL", _row("20261016", 7.0, 19, 148.32, 17179.86, 150.0)),     # 지난주 금
    ("SOXL", _row("20261019", 8.0, 22, 148.10, 16733.0, 147.0)),
    ("SOXL", _row("20261020", 9.0, 25, 147.90, 16290.0, 146.0)),
    ("SOXL", _row("20261021", 7.0, 19, 147.90, 17300.0, 160.0, pl=73.2)),  # 쿼터매도
    ("SOXL", _row("20261022", 8.0, 22, 148.50, 16850.0, 155.0)),
    ("SOXL", _row("20261023", 9.0, 25, 148.80, 16400.0, 152.0)),
]


def test_week_range_saturday():
    assert week_range(SAT) == ("20261017", "20261023")


def test_basic_week():
    sm, reg = _setup(rows=SOXL_ROWS)
    _fill(reg, "SOXL", "20261019", "buy", 3, 147.0, "star_buy")
    _fill(reg, "SOXL", "20261021", "sell", 6, 160.0, "quarter_sell")
    _fill(reg, "SOXL", "20261016", "buy", 1, 150.0, "star_buy")     # 지난주 — 빠져야 한다
    txt = weekly_report(sm, reg, SAT)
    assert "10/17 ~ 10/23" in txt
    assert "[SOXL] 5거래일" in txt
    assert "T 7.0 → 9.0 (+2.0)" in txt
    assert "매수 3주 $441.00" in txt
    assert "매도 6주 $960.00" in txt
    assert "실현손익 $+73.20" in txt
    assert "25주 @$148.80" in txt and "종가 $152.00" in txt
    assert "평가 $+80.00" in txt
    assert "합계" not in txt                     # 한 종목이면 합계 없음


def test_sparkline_includes_previous_friday():
    sm, reg = _setup(rows=SOXL_ROWS)
    txt = weekly_report(sm, reg, SAT)
    line = next(l for l in txt.splitlines() if "T 7.0 → 9.0" in l)
    assert len(line.split()[-1]) == 6               # 지난 금 + 이번 주 5일


def test_two_tickers_total_and_missing():
    rows = SOXL_ROWS + [("TQQQ", _row("20261019", 1.0, 5, 100.0, 19500.0, 101.0)),
                        ("TQQQ", _row("20261023", 3.0, 15, 99.0, 18500.0, 98.0))]
    sm, reg = _setup(("SOXL", "TQQQ"), rows)
    _fill(reg, "TQQQ", "20261019", "buy", 5, 100.0, "half_buy")
    txt = weekly_report(sm, reg, SAT)
    assert "[TQQQ] 2거래일" in txt
    assert "T 1.0 → 3.0" in txt                    # 직전 기록 없으면 첫날 T
    assert "합계" in txt and "매수 $500.00" in txt and "평가 $+65.00" in txt


def test_no_records():
    sm, reg = _setup()
    assert "이번 주 정산 기록 없음" in weekly_report(sm, reg, SAT)


def test_cycle_closed_and_reverse_flagged():
    rows = [("SOXL", _row("20261020", 30.0, 100, 150.0, 5000.0, 140.0, mode="reverse")),
            ("SOXL", _row("20261022", 0.0, 0, 0.0, 21000.0, 160.0, closed=True))]
    sm, reg = _setup(rows=rows)
    txt = weekly_report(sm, reg, SAT)
    assert "사이클 1회 완료" in txt and "보유 없음" in txt


def test_rerun_same_day_counted_once():
    sm, reg = _setup(rows=SOXL_ROWS + [SOXL_ROWS[-1]])
    assert "[SOXL] 5거래일" in weekly_report(sm, reg, SAT)


def test_month_boundary():
    rows = [("SOXL", _row("20260930", 5.0, 15, 150.0, 17000.0, 150.0)),
            ("SOXL", _row("20261001", 6.0, 18, 150.0, 16500.0, 149.0))]
    sm, reg = _setup(rows=rows)
    txt = weekly_report(sm, reg, date(2026, 10, 3))
    assert "[SOXL] 2거래일" in txt


def test_scheduled_and_command_wired():
    src = (ROOT / "scheduler" / "engine.py").read_text(encoding="utf-8")
    assert 'id="weekly_report"' in src and "WEEKLY_CRON" in src
    bot = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    assert 'CommandHandler("weekly"' in bot and "/weekly" in bot


def test_engine_send_weekly():
    from scheduler.engine import SchedulerEngine
    sm, reg = _setup(rows=SOXL_ROWS)
    sent = []

    class N:
        async def send(self, t): sent.append(t)
    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry, eng.notifier = sm, reg, N()
    asyncio.run(eng.send_weekly(SAT))
    assert sent and "주간 요약" in sent[0]


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
