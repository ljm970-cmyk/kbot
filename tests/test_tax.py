"""
================================================================
연간 실현손익 (세금 참고용)

실행:  python tests/test_tax.py
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

import csv  # noqa: E402

from core.tax import fifo_sales, format_yearly, write_csv, yearly  # noqa: E402

FEE = 0.0007


def _rows(*items):
    out = []
    for i, (d, side, q, p) in enumerate(items):
        out.append({"id": i, "trade_date": d, "side": side, "tag": "x",
                    "filled_qty": q, "filled_price": p})
    return out


def test_fifo_basic():
    rows = _rows(("20260101", "buy", 10, 100.0), ("20260102", "buy", 10, 80.0),
                 ("20260103", "sell", 15, 110.0))
    t = fifo_sales(rows, "SOXL", "2026", 0.0)
    s = t.sales[0]
    assert s.qty == 15
    assert abs(s.cost - (10 * 100 + 5 * 80)) < 1e-9        # 먼저 산 100 짜리부터
    assert abs(s.gain - (15 * 110 - 1400)) < 1e-9


def test_fees_included():
    rows = _rows(("20260101", "buy", 10, 100.0), ("20260103", "sell", 10, 110.0))
    s = fifo_sales(rows, "SOXL", "2026", 0.001).sales[0]
    assert abs(s.cost - 1001.0) < 1e-9 and abs(s.fee - 1.1) < 1e-9
    assert abs(s.gain - (1100 - 1001 - 1.1)) < 1e-9


def test_previous_year_lots_used_but_sales_filtered():
    rows = _rows(("20251201", "buy", 10, 50.0), ("20251215", "sell", 4, 60.0),
                 ("20260105", "sell", 6, 70.0))
    t = fifo_sales(rows, "SOXL", "2026", 0.0)
    assert len(t.sales) == 1 and t.sales[0].qty == 6
    assert abs(t.sales[0].cost - 300.0) < 1e-9


def test_unmatched_sell_flagged_not_counted():
    rows = _rows(("20260105", "sell", 5, 70.0))
    t = fifo_sales(rows, "TQQQ", "2026", 0.0)
    assert t.sales == [] and t.unmatched_qty == 5


def _setup():
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    reg = OrderRegistry(d / "o.db")
    sm.save_config("SOXL", {"ticker": "SOXL", "division": 40, "principal": 20000.0,
                            "fee_rate": 0.0, "is_active": True})
    return d, sm, reg


def _fill(reg, day, side, qty, price, tag):
    o = PlannedOrder(tag=tag, side=side, trade_type=TradeType.LOC, qty=qty, price=price)
    rid = reg.record_submission(day, "SOXL", o)
    with reg._conn() as c:
        c.execute("UPDATE orders SET status='filled', filled_qty=?, filled_price=? WHERE id=?",
                  (qty, price, rid))


def test_same_day_buy_before_sell():
    """종가에 별지점 매수와 쿼터매도가 함께 체결된 날 — 매수를 먼저 쌓는다"""
    d, sm, reg = _setup()
    _fill(reg, "20261021", "sell", 2, 110.0, "quarter_sell")
    _fill(reg, "20261021", "buy", 5, 100.0, "star_buy")
    rows = reg.filled_rows("SOXL")
    assert [r["side"] for r in rows] == ["buy", "sell"]


def test_yearly_with_fx_and_tax():
    d, sm, reg = _setup()
    _fill(reg, "20261001", "buy", 100, 100.0, "star_buy")
    _fill(reg, "20261020", "sell", 100, 130.0, "target_sell")
    sm.archive_eod("SOXL", "20261020", {"T": 0.0, "holdings": 0, "usd_krw": 1400.0})
    items = yearly(sm, reg, "2026")
    s = items[0].sales[0]
    assert s.usd_krw == 1400.0 and abs(s.gain - 3000.0) < 1e-6
    txt = format_yearly("2026", items)
    assert "₩+4,200,000" in txt
    assert "과세표준 ≈ ₩1,700,000" in txt and "예상세액(22%) ≈ ₩374,000" in txt
    assert "직접 매매" in txt


def test_fallback_rate_and_missing_rate():
    d, sm, reg = _setup()
    _fill(reg, "20261001", "buy", 10, 100.0, "star_buy")
    _fill(reg, "20261020", "sell", 10, 110.0, "quarter_sell")
    assert "환율 기록이 없어" in format_yearly("2026", yearly(sm, reg, "2026"))
    assert "₩+140,000" in format_yearly("2026", yearly(sm, reg, "2026", fallback_rate=1400.0))


def test_no_sales():
    d, sm, reg = _setup()
    assert "판 기록이 없습니다" in format_yearly("2026", yearly(sm, reg, "2026"))


def test_csv_written_excel_friendly():
    d, sm, reg = _setup()
    _fill(reg, "20261001", "buy", 10, 100.0, "star_buy")
    _fill(reg, "20261020", "sell", 10, 110.0, "quarter_sell")
    p = write_csv(yearly(sm, reg, "2026", fallback_rate=1400.0), d / "t.csv")
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")           # BOM — 엑셀 한글
    rows = list(csv.reader(p.read_text(encoding="utf-8-sig").splitlines()))
    assert rows[0][0] == "매도일" and rows[1][1] == "SOXL" and rows[1][-1] == "140000"


def test_tax_route_before_calc():
    src = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    assert src.index('"세금" in text') < src.index('"계산" in text')
    assert 'CommandHandler("tax", self._cmd_tax)' in src


def test_eod_archives_fx_rate():
    src = (ROOT / "scheduler" / "engine.py").read_text(encoding="utf-8")
    assert '"usd_krw": quote.get("usd_krw")' in src


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
