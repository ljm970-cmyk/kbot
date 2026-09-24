"""
================================================================
프리장 지정가매도 테스트

예약주문은 정규장 개장(22:30) 때 실주문으로 넘어간다. 지정가매도를
예약으로 걸면 프리장(17:00~22:30)에 목표가에 닿아도 팔지 못한다.
실측(2026-09-22 17:25)으로 실시간 지정가 주문이 프리장에 바로 살아
있음을 확인했다.

실행:  python tests/test_premarket.py
================================================================
"""

import asyncio
import sys
import tempfile
import types
from datetime import date, timedelta
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

from core.market_calendar import DaySchedule, premarket_open_kst, regular_open_kst  # noqa: E402
from core.order_registry import OrderRegistry  # noqa: E402
from core.state_manager import StateManager  # noqa: E402
from core.t_calculator import FillKind  # noqa: E402
from kiwoom.constants import TradeType  # noqa: E402
from modes.base_mode import PlannedOrder  # noqa: E402

CFG = {"division": 40, "principal": 20000.0, "fee_rate": 0.0007, "is_active": True}


class _Res:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Kiwoom:
    def __init__(self):
        self.calls = []

    async def sell(self, t, ex, qty, tt, price=None, stop_price=None):
        self.calls.append(("sell", tt, qty, price))
        return _Res(ord_no="000010099")

    async def reserve_sell(self, t, ex, qty, tt, price=None, **k):
        self.calls.append(("reserve_sell", tt, qty, price))
        return _Res(rsrv_ord_no="R001")

    async def reserve_moc_sell(self, t, ex, qty):
        self.calls.append(("reserve_moc_sell", qty))
        return _Res(rsrv_ord_no="R002")

    async def buy(self, t, ex, qty, tt, price=None):
        self.calls.append(("buy", tt, qty, price))
        return _Res(ord_no="000010100")

    async def reserve_buy(self, t, ex, qty, tt, price=None, **k):
        self.calls.append(("reserve_buy", tt, qty, price))
        return _Res(rsrv_ord_no="R003")


def _engine():
    from scheduler.engine import SchedulerEngine
    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.kiwoom = _Kiwoom()
    return eng


def _order(tag, side, tt, qty=3, price=180.0):
    return PlannedOrder(tag=tag, side=side, trade_type=tt, qty=qty, price=price)


# ================================================================
# 주문 경로
# ================================================================

def test_target_sell_goes_realtime():
    """지정가매도는 실시간 주문 — 프리장부터 살아 있어야 한다"""
    e = _engine()
    res = asyncio.run(e._place("SOXL", "NY",
                               _order(FillKind.TARGET_SELL, "sell", TradeType.LIMIT)))
    assert e.kiwoom.calls[0][0] == "sell"
    assert res.ord_no == "000010099"


def test_loc_sell_goes_realtime():
    """LOC 도 실시간으로 — 거부가 접수 즉시 드러나 대응할 시간이 생긴다"""
    e = _engine()
    asyncio.run(e._place("SOXL", "NY",
                         _order(FillKind.QUARTER_SELL, "sell", TradeType.LOC)))
    assert e.kiwoom.calls[0][:2] == ("sell", TradeType.LOC)


def test_moc_sell_stays_reserved():
    e = _engine()
    asyncio.run(e._place("SOXL", "NY",
                         _order(FillKind.REVERSE_SELL, "sell", TradeType.MOC, price=None)))
    assert e.kiwoom.calls[0][0] == "reserve_moc_sell"


def test_loc_buys_go_realtime():
    """실측: 실시간 LOC 가 프리장에 바로 접수된다 (현재가 −29% 도)"""
    e = _engine()
    for tag, price in [(FillKind.ENTRY_BUY, 163.11), (FillKind.CRASH_BUY, 62.50)]:
        asyncio.run(e._place("SOXL", "NY", _order(tag, "buy", TradeType.LOC, price=price)))
    assert [c[0] for c in e.kiwoom.calls] == ["buy", "buy"]
    assert all(c[1] == TradeType.LOC for c in e.kiwoom.calls)


def test_no_reserve_orders_except_moc():
    """MOC 외에는 예약주문을 쓰지 않는다"""
    e = _engine()
    for o in [_order(FillKind.TARGET_SELL, "sell", TradeType.LIMIT),
              _order(FillKind.QUARTER_SELL, "sell", TradeType.LOC),
              _order(FillKind.STAR_BUY, "buy", TradeType.LOC),
              _order(FillKind.AVG_BUY, "buy", TradeType.LOC),
              _order(FillKind.GUARD_BUY, "buy", TradeType.LOC)]:
        asyncio.run(e._place("SOXL", "NY", o))
    assert not any(c[0].startswith("reserve") for c in e.kiwoom.calls)


def test_realtime_order_number_is_recorded():
    """실시간 주문은 예약번호가 아니라 주문번호로 남겨야 체결과 바로 이어진다"""
    src = (ROOT / "scheduler" / "engine.py").read_text(encoding="utf-8")
    assert "attach_ord_no(record_id, res.ord_no)" in src
    assert "attach_rsrv_ord_no(record_id, res.rsrv_ord_no)" in src


# ================================================================
# 일정
# ================================================================

def test_target_sell_one_minute_after_premarket_open():
    """개장 정각은 세션 전환 순간이라 거부될 수 있다"""
    d = date(2026, 9, 23)
    s = DaySchedule.build(d)
    assert s.premarket == premarket_open_kst(d) + timedelta(minutes=1)
    assert s.premarket.hour == 17 and s.premarket.minute == 1


def test_loc_right_after_target_sell():
    """지정가매도 17:01 바로 다음 17:02 에 LOC — 둘 다 프리장에 함께 걸린다"""
    s = DaySchedule.build(date(2026, 9, 23))
    assert (s.premarket.hour, s.premarket.minute) == (17, 1)
    assert (s.submit_loc.hour, s.submit_loc.minute) == (17, 2)
    assert s.premarket < s.submit_loc


def test_verify_again_after_regular_open():
    """예약주문 거부는 정규장 개장 때 결정된다. 프리장 검증만으로는 못 잡는다."""
    d = date(2026, 9, 23)
    s = DaySchedule.build(d)
    assert s.verify_open == regular_open_kst(d) + timedelta(minutes=10)
    assert s.verify < s.verify_open < s.eod
    assert (s.verify_open.hour, s.verify_open.minute) == (22, 40)     # 서머타임


def test_regular_open_winter():
    s = DaySchedule.build(date(2026, 12, 15))
    assert (s.verify_open.hour, s.verify_open.minute) == (23, 40)


def test_verify_after_open_registered():
    src = (ROOT / "scheduler" / "engine.py").read_text(encoding="utf-8")
    assert '("verify_after_open", sched.verify_open, self.verify_reserved)' in src


def test_schedule_describe_shows_open_verify():
    assert "개장 검증" in DaySchedule.build(date(2026, 9, 23)).describe()


# ================================================================
# /panic — 실시간 주문도 거둔다
# ================================================================

def test_panic_cancels_realtime_bot_orders():
    from core.ops import panic_stop

    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", CFG)
    reg = OrderRegistry(d / "o.db")
    rid = reg.record_submission("20260923", "SOXL", _order(
        FillKind.TARGET_SELL, "sell", TradeType.LIMIT, qty=3, price=195.0))
    reg.attach_ord_no(rid, "000010099")

    cancelled = []

    class K:
        def can_cancel_now(self): return True
        async def get_reserved_orders(self, **k): return []
        async def get_open_orders(self, t, ex):
            return [
                {"ord_no": "000010099", "side": "sell", "remain_qty": 3, "ord_uv": 195.0},
                {"ord_no": "000077777", "side": "buy", "remain_qty": 5, "ord_uv": 50.0},
            ]
        async def cancel(self, no, t, ex):
            cancelled.append(no)
            return {}

    res = asyncio.run(panic_stop(K(), sm, reg, lambda t: "NY"))
    assert cancelled == ["000010099"], "개인 실시간 주문까지 취소됐다"
    assert any("실시간" in c for c in res.cancelled)
    assert reg.bot_live_orders("SOXL") == {}


def test_bot_live_orders_excludes_reserved_and_dead():
    d = Path(tempfile.mkdtemp())
    reg = OrderRegistry(d / "o.db")
    r1 = reg.record_submission("20260923", "SOXL", _order(
        FillKind.TARGET_SELL, "sell", TradeType.LIMIT))
    reg.attach_ord_no(r1, "A1")
    r2 = reg.record_submission("20260923", "SOXL", _order(
        FillKind.ENTRY_BUY, "buy", TradeType.LOC, price=160.0))
    reg.attach_rsrv_ord_no(r2, "R1")
    r3 = reg.record_submission("20260923", "SOXL", _order(
        FillKind.QUARTER_SELL, "sell", TradeType.LIMIT, price=170.0))
    reg.attach_ord_no(r3, "A3")
    reg.set_status(r3, "cancelled")

    live = reg.bot_live_orders("SOXL")
    assert set(live) == {"A1"}


# ================================================================
# 미체결이 없을 때 · 실시간 주문 생존 확인
# ================================================================

def test_empty_open_orders_message_is_not_an_error():
    """실측: 미체결이 없으면 '해당 계좌의미체결내역이 없습니다' 로 온다.
    에러로 처리하면 미체결 없는 날 검증과 /panic 이 깨진다."""
    from kiwoom.api_client import is_empty_result
    assert is_empty_result("[2000](571758:해당 계좌의미체결내역이 없습니다.)")
    assert is_empty_result("자료가 존재하지 않습니다")
    assert not is_empty_result("주문가능금액이 부족합니다")


def _verify_engine(open_nos):
    from scheduler.engine import SchedulerEngine
    d = Path(tempfile.mkdtemp())
    reg = OrderRegistry(d / "o.db")
    sent = []

    class K:
        async def get_open_orders(self, t, ex):
            return [{"ord_no": n} for n in open_nos]

    class N:
        async def send(self, text): sent.append(text)

    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.registry, eng.kiwoom, eng.notifier = reg, K(), N()
    return eng, reg, sent


def test_missing_loc_is_reported():
    """LOC 는 종가 전엔 체결될 수 없다. 사라졌으면 거부·취소다."""
    eng, reg, sent = _verify_engine(open_nos=[])
    rid = reg.record_submission("20260923", "SOXL",
                                _order(FillKind.ENTRY_BUY, "buy", TradeType.LOC, price=163.11))
    reg.attach_ord_no(rid, "000010100")
    asyncio.run(eng._verify_live("SOXL"))
    assert sent and "사라졌습니다" in sent[0]
    assert "163.11" in sent[0]


def test_missing_limit_sell_is_not_alarmed():
    """지정가매도는 장중에 체결될 수 있어 사라져도 이상이 아니다"""
    eng, reg, sent = _verify_engine(open_nos=[])
    rid = reg.record_submission("20260923", "SOXL",
                                _order(FillKind.TARGET_SELL, "sell", TradeType.LIMIT))
    reg.attach_ord_no(rid, "000010101")
    asyncio.run(eng._verify_live("SOXL"))
    assert sent == []


def test_live_loc_is_quiet():
    eng, reg, sent = _verify_engine(open_nos=["000010100"])
    rid = reg.record_submission("20260923", "SOXL",
                                _order(FillKind.ENTRY_BUY, "buy", TradeType.LOC, price=163.11))
    reg.attach_ord_no(rid, "000010100")
    asyncio.run(eng._verify_live("SOXL"))
    assert sent == []


# ================================================================
# 접수 성공 알림
#
# 원본은 실패·거부일 때만 알렸다. 잘 들어간 날은 조용해서 주문이
# 나갔는지 확인하려면 매번 /orders 를 쳐야 했다.
# ================================================================

def _report(placed, label="LOC 접수"):
    from scheduler.engine import SchedulerEngine
    return SchedulerEngine._submit_report("SOXL", label, placed)


def test_submit_report_lists_orders():
    text = _report([
        _order(FillKind.STAR_BUY, "buy", TradeType.LOC, qty=1, price=175.93),
        _order(FillKind.AVG_BUY, "buy", TradeType.LOC, qty=2, price=149.10),
    ])
    assert "· 2건" in text
    assert "175.93" in text and "149.10" in text
    assert "star_buy" in text and "avg_buy" in text


def test_submit_report_shows_cost_for_buys():
    text = _report([
        _order(FillKind.STAR_BUY, "buy", TradeType.LOC, qty=1, price=100.0),
        _order(FillKind.AVG_BUY, "buy", TradeType.LOC, qty=2, price=50.0),
    ])
    assert "$200.00" in text


def test_submit_report_sells_have_no_cost_line():
    text = _report([
        _order(FillKind.TARGET_SELL, "sell", TradeType.LIMIT, qty=4, price=178.92),
    ], label="지정가매도")
    assert "🔵 매도" in text
    assert "소요" not in text


def test_submit_report_shortens_order_kind():
    text = _report([_order(FillKind.STAR_BUY, "buy", TradeType.LOC, qty=1, price=1.0)])
    assert "LOC" in text
    assert "On Close" not in text


def test_submit_report_sells_first():
    """매도가 먼저 접수되므로 알림도 같은 순서로 읽힌다"""
    text = _report([
        _order(FillKind.STAR_BUY, "buy", TradeType.LOC, qty=1, price=175.93),
        _order(FillKind.QUARTER_SELL, "sell", TradeType.LOC, qty=2, price=175.94),
    ])
    assert text.index("매도") < text.index("매수")


def test_success_notification_is_sent():
    src = (ROOT / "scheduler" / "engine.py").read_text(encoding="utf-8")
    block = src[src.index("logger.info(\"[%s] %s %d/%d건 접수\""):]
    block = block[:block.index("@staticmethod")]
    assert "_submit_report" in block
    assert "elif placed:" in block


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


