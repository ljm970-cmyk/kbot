"""
================================================================
사이클 결산

  - 정산 아카이브에 사이클 종료 표시가 남는다 (예전엔 빠져 있어
    /stats 가 모든 사이클을 하나로 합쳤다)
  - 사이클이 끝난 날 결산 한 장을 따로 보낸다
  - 예전 아카이브(종료 표시 없음)도 보유 0 으로 종료를 알아챈다

실행:  python tests/test_cycle_report.py
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

FEE = 0.0007


from eod.stats import collect, format_cycle_end  # noqa: E402


def _mgr(rows, ticker="SOXL"):
    sm = StateManager(Path(tempfile.mkdtemp()) / "d")
    for r in rows:
        sm.archive_eod(ticker, r["trade_date"], r)
    return sm


def _row(d, cash, h, avg, T, closed=None):
    r = {"trade_date": d, "cash": cash, "holdings": h, "avg_price": avg, "T": T,
         "mode": "normal", "realized_pl": 0.0}
    if closed is not None:
        r["cycle_closed"] = closed
    return r


ROWS = [
    _row("20260922", 19544.15, 3, 151.95, 1.0, False),
    _row("20260923", 19105.40, 6, 149.10, 2.0, False),
    _row("20260924", 18660.10, 9, 148.80, 3.0, False),
    _row("20260925", 20702.33, 0, 0.0, 0.0, True),
]


# ================================================================
# 결산 문구
# ================================================================

def test_cycle_end_text():
    s = collect(_mgr(ROWS), "SOXL", ["202609"])
    txt = format_cycle_end(s, principal=20000.0, division=40, t_values=[1.0, 2.0, 3.0])
    assert "사이클 #1 완료" in txt
    assert "9/22 ~ 9/25 · 4거래일" in txt
    assert "최대 T   3.0 / 40" in txt
    assert "+702.33" in txt and "+3.51%" in txt and "원금 대비" in txt
    assert "누적 1회" in txt
    assert "T 흐름" in txt


def test_losing_cycle_marked():
    rows = ROWS[:-1] + [_row("20260925", 19800.0, 0, 0.0, 0.0, True)]
    txt = format_cycle_end(collect(_mgr(rows), "SOXL", ["202609"]), 20000.0, 40)
    assert "🥶" in txt and "$-200.00" in txt


def test_second_half_flag():
    rows = [_row("20260901", 10000.0, 60, 150.0, 22.0, False),
            _row("20260902", 21000.0, 0, 0.0, 0.0, True)]
    txt = format_cycle_end(collect(_mgr(rows), "SOXL", ["202609"]), 20000.0, 40)
    assert "후반전 진입" in txt


def test_no_cycles_empty():
    assert format_cycle_end(collect(_mgr(ROWS[:2]), "SOXL", ["202609"])) == ""


# ================================================================
# 아카이브·집계
# ================================================================

def test_old_archive_without_flag_still_splits():
    """예전 아카이브에는 cycle_closed 가 없다. 보유가 0 이 되면 종료로 본다."""
    rows = [{k: v for k, v in r.items() if k != "cycle_closed"} for r in ROWS]
    rows += [_row("20260926", 20202.33, 3, 166.0, 1.0)]
    for r in rows:
        r.pop("cycle_closed", None)
    s = collect(_mgr(rows), "SOXL", ["202609"])
    assert s.closed == 1
    assert s.open_cycle is not None and s.open_cycle.start_date == "20260926"


def test_rerun_eod_same_day_counted_once():
    rows = ROWS[:2] + [ROWS[1]] + ROWS[2:]
    s = collect(_mgr(rows), "SOXL", ["202609"])
    assert s.cycles[0].days == 4


def test_t_values_between():
    from core.tchart import t_values_between
    sm = _mgr(ROWS)
    assert t_values_between(sm, "SOXL", "20260922", "20260925") == [1.0, 2.0, 3.0, 0.0]
    assert t_values_between(sm, "SOXL", "20260923", "20260924") == [2.0, 3.0]


# ================================================================
# 정산 — 실제 흐름
# ================================================================

class _Res:
    def __init__(self, closed):
        self.cycle_closed = closed
        self.fills_applied = 1
        self.realized_pl = 0.0
        self.anomalies = []
        self.unmatched_fills = []


def _eod_engine(closed):
    import scheduler.engine as E
    from scheduler.engine import SchedulerEngine
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", {"ticker": "SOXL", "division": 40, "principal": 20000.0,
                            "fee_rate": FEE, "is_active": True})
    for r in ROWS[:3]:
        sm.archive_eod("SOXL", r["trade_date"], r)
    st = sm.get_state("SOXL")
    st.T, st.holdings, st.avg_price, st.cash = 3.0, 9, 148.80, 18660.10
    sm.save_state(st)
    sent = []

    class K:
        async def get_today_orders(self, t, ex): return []
        async def get_quote(self, t, ex): return {"cur_price": 160.0, "prev_close": 158.0}
        async def get_recent_closes(self, t, ex, n=5): return [160.0] * 5

    class Eod:
        def run(self, state, trade_date, fills, close, market):
            if closed:
                state.cash, state.holdings, state.avg_price, state.T = 20702.33, 0, 0.0, 0.0
            return _Res(closed)

    class N:
        async def send(self, text): sent.append(text)

    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry = sm, OrderRegistry(d / "o.db")
    eng.kiwoom, eng.notifier, eng.eod, eng.fill_source = K(), N(), Eod(), None
    eng.config = type("C", (), {"dry_run": False})()

    async def no_reconcile(*a, **k): return None
    eng._reconcile_ticker = no_reconcile
    E.daily_report = lambda *a, **k: "일일 리포트"
    return eng, sm, sent


def test_eod_archives_flag_and_sends_cycle_report():
    eng, sm, sent = _eod_engine(closed=True)
    asyncio.run(eng._eod_ticker("SOXL", "20260925", date(2026, 9, 25)))
    last = sm.read_archive("202609", "SOXL")[-1]
    assert last["cycle_closed"] is True and last["division"] == 40
    assert any("사이클 #1 완료" in m for m in sent)
    rep = next(m for m in sent if "사이클 #1 완료" in m)
    assert "T 흐름" in rep


def test_eod_no_report_mid_cycle():
    eng, sm, sent = _eod_engine(closed=False)
    asyncio.run(eng._eod_ticker("SOXL", "20260925", date(2026, 9, 25)))
    assert sm.read_archive("202609", "SOXL")[-1]["cycle_closed"] is False
    assert not any("완료" in m for m in sent)


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
