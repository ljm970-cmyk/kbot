"""
================================================================
리버스 대비 — 접근 알림, MOC 대체 주문, MOC 경로 점검

실행:  python tests/test_reverse_guard.py
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

from datetime import datetime  # noqa: E402

from core import reverse_guard as rg  # noqa: E402
from kiwoom.api_client import KiwoomAPIError  # noqa: E402

FEE = 0.0007
KST = rg.KST


class _St:
    def __init__(self, T, division=40, mode="normal"):
        self.T, self.division, self.mode = T, division, mode


# ================================================================
# 1. 접근 알림
# ================================================================

def test_approach_text_thresholds():
    assert rg.approach_text(_St(34.9)) == ""
    txt = rg.approach_text(_St(35.0))
    assert "리버스 접근" in txt and "T>39" in txt and "4.0" in txt and "4거래일" in txt
    assert rg.approach_text(_St(36.5, mode="reverse")) == ""
    assert "T>19" in rg.approach_text(_St(15.0, division=20))
    assert rg.approach_text(_St(14.5, division=20)) == ""


def test_crossed_approach_once():
    assert rg.crossed_approach(34.5, 35.5, 40, "normal")
    assert not rg.crossed_approach(35.5, 36.5, 40, "normal")     # 이미 넘은 뒤
    assert not rg.crossed_approach(34.5, 35.5, 40, "reverse")
    assert not rg.crossed_approach(30.0, 31.0, 40, "normal")


def test_fallback_price_floor():
    assert rg.fallback_price(100.0) == 75.0
    assert rg.fallback_price(41.67) == 31.25          # 내림


def test_fallback_order_keeps_tag_qty():
    o = PlannedOrder(tag=FillKind.REVERSE_MOC_SELL, side="sell", trade_type=TradeType.MOC,
                     qty=25, price=None, note="리버스 첫날")
    fb = rg.fallback_order(o, 120.0)
    assert fb.tag == o.tag and fb.qty == 25 and fb.side == "sell"
    assert fb.trade_type == TradeType.LOC and fb.price == 90.0
    assert "MOC 거부 대체" in fb.note


# ================================================================
# 3. MOC 경로 점검
# ================================================================

class _KCheck:
    def __init__(self, reject=False, cancel_ok=True, err=""):
        self.reject, self.cancel_ok, self.err = reject, cancel_ok, err
        self.rows, self.calls = [], []

    async def reserve_moc_sell(self, t, ex, qty):
        self.calls.append(("reserve", t, qty))
        if self.reject:
            raise KiwoomAPIError("ust21201", "거래유형 오류", 400)
        self.rows = [{"rsrv_ord_no": "R1", "rsrv_dt": "20261021", "proc_tp": "미처리",
                      "rsrv_cncl_yn": "미취소", "ord_qty": "1", "err_cntn": self.err}]
        return type("R", (), {"rsrv_ord_no": "R1", "frcs_dt": ""})()

    async def get_reserved_orders(self, ticker="", exchange="", include_cancelled=False):
        return list(self.rows)

    async def cancel_reserved(self, rsrv_dt, no, t, ex):
        self.calls.append(("cancel", rsrv_dt, no))
        if not self.cancel_ok:
            raise KiwoomAPIError("ust21203", "취소 불가", 400)
        self.rows = [dict(r, rsrv_cncl_yn="취소") for r in self.rows]
        return {}


NOON = datetime(2026, 10, 21, 12, 0, tzinfo=KST)


def _check(k, now=NOON):
    return asyncio.run(rg.run_moc_check(k, "SOXL", "NASD", now=now))


def test_moc_check_success():
    k = _KCheck()
    txt = _check(k)
    assert "접수됨" in txt and "예약 목록 확인" in txt and "취소 확인" in txt
    assert "경로는 정상" in txt
    assert ("cancel", "20261021", "R1") in k.calls
    assert ("reserve", "SOXL", 1) in k.calls


def test_moc_check_reject_at_submit():
    txt = _check(_KCheck(reject=True))
    assert "접수 거부" in txt and "자동 대체" in txt


def test_moc_check_list_error_reported():
    txt = _check(_KCheck(err="주문유형 불가"))
    assert "에러로 표시" in txt and "주문유형 불가" in txt


def test_moc_check_cancel_failure_warns_loudly():
    txt = _check(_KCheck(cancel_ok=False))
    assert "취소 실패" in txt and "남아 있을 수 있습니다" in txt and "22:25" in txt


def test_moc_check_window():
    k = _KCheck()
    for h, m in [(7, 59), (22, 10), (23, 0)]:
        txt = _check(k, datetime(2026, 10, 21, h, m, tzinfo=KST))
        assert "하지 않았습니다" in txt
    assert k.calls == []
    assert "접수됨" in _check(k, datetime(2026, 10, 21, 22, 9, tzinfo=KST))


# ================================================================
# 2. MOC 대체 — 실제 접수 흐름
# ================================================================

def _rev_engine(reserve_raises=False, sell_raises=False):
    from scheduler.engine import SchedulerEngine
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", {"ticker": "SOXL", "division": 40, "principal": 20000.0,
                            "fee_rate": FEE, "is_active": True})
    st = sm.get_state("SOXL")
    st.T, st.holdings, st.avg_price, st.cash = 39.5, 140, 140.0, 300.0
    st.mode, st.reverse_first_day = "reverse", True
    sm.save_state(st)
    sent, live, reserved = [], [], []

    class K:
        async def available_usd(self, t, ex, price): return 300.0
        async def get_quote(self, t, ex): return {"cur_price": 100.0, "prev_close": 100.0}
        async def get_recent_closes(self, t, ex, n=5): return [100.0] * 5
        async def get_open_orders(self, t, ex): return []

        async def reserve_moc_sell(self, t, ex, qty):
            if reserve_raises:
                raise KiwoomAPIError("ust21201", "MOC 불가", 400)
            reserved.append(qty)
            return type("R", (), {"rsrv_ord_no": "RS1"})()

        async def sell(self, t, ex, qty, tt, price=None, stop_price=None):
            if sell_raises:
                raise KiwoomAPIError("ust20001", "가격 범위", 400)
            live.append((qty, tt, price))
            return type("O", (), {"ord_no": f"L{len(live)}"})()

        async def buy(self, *a, **k):
            raise AssertionError("리버스 첫날은 매수가 없다")

    class N:
        async def send(self, text): sent.append(text)

    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry = sm, OrderRegistry(d / "o.db")
    eng.kiwoom, eng.notifier = K(), N()
    eng.config = type("C", (), {"dry_run": False})()
    return eng, sent, live, reserved


def _submit(eng):
    from scheduler.engine import SubmitWindow
    asyncio.run(eng._submit_ticker("SOXL", "20261021", SubmitWindow.REGULAR, "LOC 접수"))


def test_moc_normal_path_untouched():
    eng, sent, live, reserved = _rev_engine()
    _submit(eng)
    assert reserved and not any(tt == TradeType.LOC and p == 75.0 for _, tt, p in live)
    assert not any("대체" in s for s in sent)


def test_moc_rejected_at_submit_falls_back_to_loc():
    eng, sent, live, _ = _rev_engine(reserve_raises=True)
    _submit(eng)
    fb = [x for x in live if x[1] == TradeType.LOC and x[2] == 75.0]
    assert len(fb) == 1
    assert any("LOC 매도로 대체" in s for s in sent)
    rows = eng.registry.day_orders("20261021", "SOXL")
    tags = {(r.tag, r.trade_type, r.status) for r in rows}
    assert (FillKind.REVERSE_MOC_SELL, TradeType.MOC, "rejected") in tags
    assert (FillKind.REVERSE_MOC_SELL, TradeType.LOC, "submitted") in tags
    # 접수 알림에 대체 주문이 들어간다
    assert any(s.startswith("📤 [SOXL]") and "@75.00" in s for s in sent)


def test_fallback_failure_tells_user_to_sell_manually():
    eng, sent, live, _ = _rev_engine(reserve_raises=True, sell_raises=True)
    _submit(eng)
    assert any("대체 LOC 매도도 실패" in s and "직접 매도" in s for s in sent)


def test_moc_rejected_at_open_falls_back():
    """22:30 실주문 전환 때 거부 — 개장 후 검증에서 잡아 대체한다"""
    eng, sent, live, _ = _rev_engine()
    _submit(eng)
    moc = [r for r in eng.registry.day_orders("20261021", "SOXL")
           if r.trade_type == TradeType.MOC][0]

    async def bad(t, ex):
        return [{"rsrv_ord_no": "RS1", "ord_qty": moc.qty, "ord_uv": 0.0,
                 "reason": "증거금 부족", "proc_tp": "처리중에러"}]
    eng.kiwoom.verify_reserved_orders = bad

    async def nolive(t): return None
    eng._verify_live = nolive
    sent.clear()
    asyncio.run(eng.verify_reserved(date(2026, 10, 21)))
    assert any("예약주문 1건 거부" in s for s in sent)
    assert any("LOC 매도로 대체" in s and "증거금 부족" in s for s in sent)
    assert [x for x in live if x[2] == 75.0]

    # 두 번째 검증에서는 다시 대체하지 않는다
    n = len(live)
    asyncio.run(eng.verify_reserved(date(2026, 10, 21)))
    assert len(live) == n


def test_non_moc_reserve_rejection_no_fallback():
    eng, sent, live, _ = _rev_engine()
    rid = eng.registry.record_submission("20261021", "SOXL", PlannedOrder(
        tag=FillKind.TARGET_SELL if hasattr(FillKind, "TARGET_SELL") else "target_sell",
        side="sell", trade_type=TradeType.LIMIT, qty=3, price=200.0))
    eng.registry.attach_rsrv_ord_no(rid, "RX")

    async def bad(t, ex):
        return [{"rsrv_ord_no": "RX", "ord_qty": 3, "ord_uv": 200.0, "reason": "x", "proc_tp": ""}]
    eng.kiwoom.verify_reserved_orders = bad

    async def nolive(t): return None
    eng._verify_live = nolive
    asyncio.run(eng.verify_reserved(date(2026, 10, 21)))
    assert live == []


# ================================================================
# 접근 알림 — 정산·아침 요약
# ================================================================

def test_morning_brief_shows_approach():
    from core.ops import morning_brief
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", {"ticker": "SOXL", "division": 40, "principal": 20000.0,
                            "fee_rate": FEE, "is_active": True})
    st = sm.get_state("SOXL")
    st.T, st.holdings, st.avg_price, st.cash = 36.0, 130, 140.0, 1800.0
    sm.save_state(st)
    txt = morning_brief(sm, OrderRegistry(d / "o.db"), None, 5000.0)
    assert "리버스 접근" in txt and "/check moc" in txt


def test_eod_crossing_alert():
    import scheduler.engine as E
    from scheduler.engine import SchedulerEngine
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", {"ticker": "SOXL", "division": 40, "principal": 20000.0,
                            "fee_rate": FEE, "is_active": True})
    st = sm.get_state("SOXL")
    st.T, st.holdings, st.avg_price, st.cash = 34.5, 120, 140.0, 3000.0
    sm.save_state(st)
    sent = []

    class R:
        cycle_closed, fills_applied, realized_pl = False, 1, 0.0
        anomalies, unmatched_fills = [], []

    class Eod:
        def __init__(self, to): self.to = to
        def run(self, state, *a):
            state.T = self.to
            return R()

    class K:
        async def get_today_orders(self, t, ex): return []
        async def get_quote(self, t, ex): return {"cur_price": 140.0, "prev_close": 140.0}
        async def get_recent_closes(self, t, ex, n=5): return [140.0] * 5

    class N:
        async def send(self, text): sent.append(text)

    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry = sm, OrderRegistry(d / "o.db")
    eng.kiwoom, eng.notifier, eng.fill_source = K(), N(), None
    eng.config = type("C", (), {"dry_run": False})()

    async def no_rec(*a, **k): return None
    eng._reconcile_ticker = no_rec
    E.daily_report = lambda *a, **k: "리포트"

    eng.eod = Eod(35.5)
    asyncio.run(eng._eod_ticker("SOXL", "20261021", date(2026, 10, 21)))
    assert sum("리버스 접근" in s for s in sent) == 1
    sent.clear()
    eng.eod = Eod(36.5)
    asyncio.run(eng._eod_ticker("SOXL", "20261022", date(2026, 10, 22)))
    assert not any("리버스 접근" in s for s in sent)


def test_check_command_wired():
    src = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    assert 'CommandHandler("check", self._cmd_check)' in src
    assert "/check moc" in src


def test_moc_check_empty_reserve_number_warns_without_cancel():
    class K(_KCheck):
        async def reserve_moc_sell(self, t, ex, qty):
            self.calls.append(("reserve", t, qty))
            return type("R", (), {"rsrv_ord_no": "", "frcs_dt": ""})()
    k = K()
    txt = _check(k)
    assert "예약번호가 없습니다" in txt and "22:25" in txt
    assert not any(c[0] == "cancel" for c in k.calls)


def test_moc_check_not_found_is_not_cancel_confirmed():
    """조회에서 못 찾은 주문은, 취소 뒤에도 안 보인다고 취소된 것으로 보지 않는다"""
    class K(_KCheck):
        async def get_reserved_orders(self, ticker="", exchange="", include_cancelled=False):
            return []
    txt = _check(K())
    assert "취소 확인" not in txt and "남아 있을 수 있습니다" in txt


def test_fallback_uncertain_halts_and_does_not_say_sell():
    from kiwoom.api_client import KiwoomOrderUncertainError
    from core.reconciler import CircuitBreaker
    eng, sent, live, _ = _rev_engine(reserve_raises=True)

    async def unsure(*a, **k):
        raise KiwoomOrderUncertainError("ust20001", TimeoutError("timeout"))
    eng.kiwoom.sell = unsure
    _submit(eng)
    msg = next(s for s in sent if "결과를 확인할 수 없습니다" in s)
    assert "직접 매도하지 말고" in msg
    assert CircuitBreaker.is_halted(eng.state_mgr.get_state("SOXL"))
    rows = [r for r in eng.registry.day_orders("20261021", "SOXL") if r.trade_type == TradeType.LOC]
    assert rows and rows[0].status == "submitted"


def test_old_day_moc_rejection_not_replaced_today():
    eng, sent, live, _ = _rev_engine()
    rid = eng.registry.record_submission("20261020", "SOXL", PlannedOrder(
        tag=FillKind.REVERSE_MOC_SELL, side="sell", trade_type=TradeType.MOC, qty=30, price=None))
    eng.registry.attach_rsrv_ord_no(rid, "OLD1")

    async def bad(t, ex):
        return [{"rsrv_ord_no": "OLD1", "ord_qty": 30, "ord_uv": 0.0, "reason": "무효", "proc_tp": ""}]
    eng.kiwoom.verify_reserved_orders = bad

    async def nolive(t): return None
    eng._verify_live = nolive
    asyncio.run(eng.verify_reserved(date(2026, 10, 21)))
    assert live == []
    assert not any("대체" in s for s in sent)


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
