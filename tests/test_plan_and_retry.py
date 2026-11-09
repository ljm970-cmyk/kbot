"""
================================================================
plan_day 실제 실행 · LOC 자동 재시도 · 원화주문 진단

plan_day 는 매일 일정을 등록하는 핵심 함수인데 실행하는 테스트가 없었다.
여기서 실제로 돌려, 이름 오류 같은 실수로 하루 일정이 통째로 빠지는
일을 막는다.

실행:  python tests/test_plan_and_retry.py
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


class _Sched:
    def __init__(self):
        self.jobs = {}

    def add_job(self, fn, trigger, id, **k):
        self.jobs[id] = (fn, trigger, k)


def test_plan_day_registers_all_jobs():
    import scheduler.engine as E
    from scheduler.engine import SchedulerEngine
    orig_up, orig_trig = E.upcoming_session, E.DateTrigger
    E.upcoming_session = lambda: date(2030, 1, 2)
    E.DateTrigger = lambda **k: k
    sent = []

    class N:
        async def send(self, t): sent.append(t)
    try:
        eng = SchedulerEngine.__new__(SchedulerEngine)
        eng._current = None
        eng.scheduler = _Sched()
        eng.notifier = N()
        asyncio.run(eng.plan_day())
    finally:
        E.upcoming_session, E.DateTrigger = orig_up, orig_trig
    ids = set(eng.scheduler.jobs)
    for want in ("submit_target_sell", "submit_loc_orders", "verify_reserved", "verify_after_open",
                 "run_eod", "funding_reminder", "loc_retry_1", "loc_retry_2", "loc_retry_3",
                 "recover_rejected_1", "recover_rejected_2", "recover_rejected_3"):
        assert want in ids, want
    import inspect
    for fn, _, _ in eng.scheduler.jobs.values():
        assert inspect.iscoroutinefunction(fn), fn
    # 시각 순서 — 재시도는 접수 뒤, 마지막 재시도는 개장 전
    t = {k: v[1]["run_date"] for k, v in eng.scheduler.jobs.items()}
    assert t["submit_loc_orders"] < t["loc_retry_1"] < t["loc_retry_2"] < t["loc_retry_3"]
    assert t["loc_retry_3"] < t["verify_after_open"]
    from core.market_calendar import market_close_kst, regular_open_kst
    follows = sorted(v for k, v in t.items() if k.startswith("guard_follow_"))
    assert len(follows) >= 8
    assert follows[0] > t["verify_after_open"] and follows[0] > regular_open_kst(date(2030, 1, 2))
    assert follows[-1] <= market_close_kst(date(2030, 1, 2)) - E.timedelta(minutes=25)


# ================================================================
# 자동 재시도
# ================================================================

def _engine(cash):
    from scheduler.engine import SchedulerEngine
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", {"ticker": "SOXL", "division": 40, "principal": 20000.0,
                            "fee_rate": FEE, "is_active": True})
    st = sm.get_state("SOXL")
    st.T, st.holdings, st.avg_price = 2.0, 6, 149.10
    sm.save_state(st)
    placed, sent, live = [], [], set()
    wallet = {"cash": cash, "krw_set": 2935437, "krw_eval": 0.0}

    class K:
        async def available_usd(self, t, ex, price): return wallet["cash"]
        async def get_quote(self, t, ex): return {"cur_price": 146.25, "prev_close": 146.25}
        async def get_recent_closes(self, t, ex, n=5): return [146.25] * 5
        async def get_open_orders(self, t, ex): return [{"ord_no": n} for n in live]
        async def get_today_orders(self, t, ex): return []

        async def get_buying_power(self, t, ex, price):
            return {"krw_order": True, "krw_eval_usd": wallet["krw_eval"],
                    "cash_no_margin": wallet["cash"],
                    "raw": {"krw_ord_set_amt": f"{wallet['krw_set']:012d}"}}

    class N:
        async def send(self, text): sent.append(text)

    class R:
        def __init__(self, n): self.ord_no = n

    async def _place(t, ex, order):
        placed.append(order)
        no = f"N{len(placed)}"
        live.add(no)
        if order.side == "buy":
            wallet["cash"] -= order.amount * 1.006
        return R(no)

    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry = sm, OrderRegistry(d / "o.db")
    eng.kiwoom, eng.notifier, eng._place = K(), N(), _place
    eng.config = type("C", (), {"dry_run": False})()
    return eng, placed, sent, wallet, live


DAY = date(2026, 11, 5)


def test_short_at_1802_then_retry_places_missing_buys():
    from scheduler.engine import SubmitWindow
    eng, placed, sent, wallet, _ = _engine(cash=10.0)
    asyncio.run(eng._submit(DAY, SubmitWindow.REGULAR, "LOC 접수"))
    first_buys = [o for o in placed if o.side == "buy"]
    assert first_buys == []
    note = next(s for s in sent if "부족" in s)
    assert "원화주문" in note and "2,935,437원" in note and "평가가 0" in note

    wallet["cash"] = 100_000.0                    # 원화가 반영됨
    sent.clear()
    placed.clear()
    asyncio.run(eng._loc_retry_1(DAY))
    assert any(o.tag == FillKind.STAR_BUY for o in placed)
    assert all(o.side == "buy" for o in placed)   # 재시도는 매수만
    assert any("LOC 자동 재시도" in s for s in sent)


def test_retry_silent_when_nothing_missing():
    from scheduler.engine import SubmitWindow
    eng, placed, sent, wallet, _ = _engine(cash=100_000.0)
    asyncio.run(eng._submit(DAY, SubmitWindow.REGULAR, "LOC 접수"))
    sent.clear()
    placed.clear()
    asyncio.run(eng._loc_retry_1(DAY))
    assert placed == [] and sent == []


def test_retry_still_short_quiet_until_final():
    from scheduler.engine import SubmitWindow
    eng, placed, sent, wallet, _ = _engine(cash=10.0)
    asyncio.run(eng._submit(DAY, SubmitWindow.REGULAR, "LOC 접수"))
    sent.clear()
    asyncio.run(eng._loc_retry_1(DAY))
    asyncio.run(eng._loc_retry_2(DAY))
    assert sent == []
    asyncio.run(eng._loc_retry_final(DAY))
    assert any("부족" in s and "자동 재시도" in s for s in sent)


def test_retry_does_not_revive_user_cancelled_order():
    """앱에서 직접 취소한 주문은 자동 재시도가 되살리지 않는다"""
    from scheduler.engine import SubmitWindow
    eng, placed, sent, wallet, live = _engine(cash=100_000.0)
    asyncio.run(eng._submit(DAY, SubmitWindow.REGULAR, "LOC 접수"))
    star = next(r for r in eng.registry.day_orders("20261105", "SOXL") if r.tag == FillKind.STAR_BUY)
    eng.registry.set_status(star.id, "cancelled", "직접 취소")
    live.discard(star.ord_no)
    placed.clear()
    asyncio.run(eng._loc_retry_1(DAY))
    assert not any(o.tag == FillKind.STAR_BUY for o in placed)


def test_retry_leaves_broker_rejected_to_recovery():
    from scheduler.engine import SubmitWindow
    eng, placed, sent, wallet, live = _engine(cash=100_000.0)
    asyncio.run(eng._submit(DAY, SubmitWindow.REGULAR, "LOC 접수"))
    star = next(r for r in eng.registry.day_orders("20261105", "SOXL") if r.tag == FillKind.STAR_BUY)
    eng.registry.set_status(star.id, "rejected", "별지점 · 증권사 거부")
    placed.clear()
    asyncio.run(eng._loc_retry_1(DAY))
    assert not any(o.tag == FillKind.STAR_BUY for o in placed)



def _loc(tag, qty, price):
    return PlannedOrder(tag=tag, side="buy", trade_type=TradeType.LOC, qty=qty, price=price)


def test_retry_does_not_rebuy_after_guard_buy_placed():
    """실제 2026-11-09 TQQQ: 18:02 에 가격제한 대체 단건(8주)이 나갔는데
    22:00 재시도가 바뀐 시세로 별지점 4 + 평단 4 를 다시 계산해 또 내려 했다."""
    eng, placed, sent, wallet, live = _engine(cash=100_000.0)
    rid = eng.registry.record_submission("20261105", "SOXL", _loc(FillKind.GUARD_BUY, 8, 160.0), ord_no="G1")
    live.add("G1")
    asyncio.run(eng._loc_retry_1(DAY))
    main = {FillKind.STAR_BUY, FillKind.AVG_BUY, FillKind.HALF_BUY, FillKind.ENTRY_BUY,
            FillKind.GUARD_BUY, FillKind.MERGED_BUY}
    assert not any(o.tag in main for o in placed), [o.tag for o in placed]
    assert rid


def test_star_avg_placed_blocks_guard_variant():
    eng, *_ = _engine(cash=100_000.0)
    eng.registry.record_submission("20261105", "SOXL", _loc(FillKind.STAR_BUY, 4, 150.0), ord_no="S1")
    out, dup = eng._drop_already_submitted("SOXL", "20261105", [_loc(FillKind.GUARD_BUY, 8, 160.0)])
    assert out == [] and FillKind.GUARD_BUY in dup


def test_half_placed_star_still_completes_avg():
    """별지점만 들어가고 평단이 빠졌으면 평단은 채운다 (모양이 같을 때는 태그별)"""
    eng, *_ = _engine(cash=100_000.0)
    eng.registry.record_submission("20261105", "SOXL", _loc(FillKind.STAR_BUY, 4, 150.0), ord_no="S1")
    plan = [_loc(FillKind.STAR_BUY, 4, 150.0), _loc(FillKind.AVG_BUY, 4, 145.0)]
    out, _ = eng._drop_already_submitted("SOXL", "20261105", plan)
    assert [o.tag for o in out] == [FillKind.AVG_BUY]


def test_rejected_guard_does_not_block_core():
    """증권사가 거부한 대체 단건은 이미 낸 것으로 보지 않는다 (복구 흐름 몫)"""
    eng, *_ = _engine(cash=100_000.0)
    rid = eng.registry.record_submission("20261105", "SOXL", _loc(FillKind.GUARD_BUY, 8, 160.0), ord_no="G1")
    eng.registry.set_status(rid, "rejected", "거부")
    plan = [_loc(FillKind.STAR_BUY, 4, 150.0)]
    out, _ = eng._drop_already_submitted("SOXL", "20261105", plan)
    assert [o.tag for o in out] == [FillKind.STAR_BUY]



# ================================================================
# 장중 가격제한 대체 주문 따라잡기
# ================================================================

def _follow_engine(cur, guard_price=138.00, guard_qty=3, cancel_ok=True, gone=True, reject=()):
    """전반전 T=2, 평단 149.10 — 별지점 매수가 175.93, 평단 149.10, 1회 500달러.
    18:02 현재가 120 → 대체 138.00 3주가 나가 있는 상태."""
    from scheduler.engine import SchedulerEngine
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", {"ticker": "SOXL", "division": 40, "principal": 20000.0,
                            "fee_rate": FEE, "is_active": True})
    st = sm.get_state("SOXL")
    st.T, st.holdings, st.avg_price = 2.0, 6, 149.10
    sm.save_state(st)
    reg = OrderRegistry(d / "o.db")
    reg.record_submission("20261109", "SOXL", _loc(FillKind.GUARD_BUY, guard_qty, guard_price), ord_no="G1")
    live = {"G1"}
    log = []

    class K:
        async def get_quote(self, t, ex): return {"cur_price": cur, "prev_close": 120.0}
        async def get_recent_closes(self, t, ex, n=5): return [120.0] * 5
        async def get_open_orders(self, t, ex): return [{"ord_no": n} for n in live]

        async def cancel(self, no, t, ex):
            log.append(("cancel", no))
            if not cancel_ok:
                raise RuntimeError("취소 불가")
            if gone:
                live.discard(no)
            return {}

        async def buy(self, t, ex, qty, tt, price):
            from kiwoom.api_client import KiwoomAPIError
            log.append(("buy", qty, price))
            if price in reject:
                raise KiwoomAPIError("ust20000", 1, "가격 범위 초과")
            n = f"N{len(log)}"
            live.add(n)
            return types.SimpleNamespace(ord_no=n)

    sent = []

    class N:
        async def send(self, text): sent.append(text)

    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry = sm, reg
    eng.kiwoom, eng.notifier = K(), N()
    eng.config = type("C", (), {"dry_run": False})()
    eng._cancel_waits = (0,)
    return eng, log, sent, live


def _buys(log):
    return [x for x in log if x[0] == "buy"]


def test_follow_leaves_guard_when_still_safe():
    eng, log, sent, _ = _follow_engine(cur=125.0)          # 125×1.08 = 135 < 138
    asyncio.run(eng._follow_ticker("SOXL", "20261109"))
    assert log == [] and sent == []


def test_follow_swaps_back_to_star_avg_when_guard_not_needed():
    """실제 2026-11-09 SOXL 처럼 장중 크게 올라 대체가가 현재가 아래로 내려온 경우"""
    eng, log, sent, live = _follow_engine(cur=145.0)       # 별지점 175.93 < 145×1.25 → 대체 불필요
    asyncio.run(eng._follow_ticker("SOXL", "20261109"))
    assert log[0] == ("cancel", "G1")
    rows = eng.registry.day_orders("20261109", "SOXL")
    tags = sorted(r.tag for r in rows if r.status == "submitted")
    assert tags == sorted([FillKind.STAR_BUY, FillKind.AVG_BUY]), tags
    assert next(r for r in rows if r.tag == FillKind.GUARD_BUY).status == "cancelled"
    assert all(p > 138.0 for _, _, p in _buys(log))
    assert any("원래 주문으로 되돌렸습니다" in s for s in sent)
    # 한 번 더 돌아도 아무것도 안 한다
    n = len(log)
    asyncio.run(eng._follow_ticker("SOXL", "20261109"))
    assert len(log) == n


def test_follow_raises_guard_when_still_needed():
    eng, log, sent, _ = _follow_engine(cur=130.0)          # 별지점 175.93 ≥ 162.5 → 여전히 대체
    asyncio.run(eng._follow_ticker("SOXL", "20261109"))
    b = _buys(log)
    assert len(b) == 1 and b[0][2] == 149.5 and b[0][1] == 3   # 130×1.15, 500/149.5
    live_rows = [r for r in eng.registry.day_orders("20261109", "SOXL") if r.status == "submitted"]
    assert [r.tag for r in live_rows] == [FillKind.GUARD_BUY]
    assert b[0][2] < 175.93                                   # 원래 별지점을 넘지 않는다
    assert any("현재가×1.15 로 올렸습니다" in s for s in sent)


def test_follow_never_lowers():
    eng, log, sent, _ = _follow_engine(cur=110.0, guard_price=120.0)   # 110×1.08 < 120 → 안전
    asyncio.run(eng._follow_ticker("SOXL", "20261109"))
    assert log == []


def test_follow_cancel_failure_keeps_old():
    eng, log, sent, _ = _follow_engine(cur=140.0, cancel_ok=False)
    asyncio.run(eng._follow_ticker("SOXL", "20261109"))
    assert _buys(log) == []
    g = next(r for r in eng.registry.day_orders("20261109", "SOXL") if r.tag == FillKind.GUARD_BUY)
    assert g.status == "submitted"
    assert any("취소가 안 됐습니다" in s for s in sent)


def test_follow_cancel_not_confirmed_places_nothing():
    eng, log, sent, _ = _follow_engine(cur=140.0, gone=False)
    asyncio.run(eng._follow_ticker("SOXL", "20261109"))
    assert _buys(log) == []
    assert any("아직 안 빠졌습니다" in s for s in sent)


def test_follow_all_rejected_restores_old_guard():
    eng, log, sent, _ = _follow_engine(cur=145.0, reject=(175.93, 149.1))
    asyncio.run(eng._follow_ticker("SOXL", "20261109"))
    b = _buys(log)
    assert b[-1] == ("buy", 3, 138.0)
    live_rows = [r for r in eng.registry.day_orders("20261109", "SOXL") if r.status == "submitted"]
    assert [(r.tag, r.qty, r.price) for r in live_rows] == [(FillKind.GUARD_BUY, 3, 138.0)]
    assert any("다시 냈습니다" in s for s in sent)


def test_follow_skips_outside_session_window():
    """늦게 깨어난 작업이 마감 직전·장 마감 뒤에 주문을 바꾸지 않는다"""
    eng, log, sent, _ = _follow_engine(cur=140.0)
    asyncio.run(eng.follow_guard(date(2020, 1, 2)))         # 한참 지난 날
    assert log == []


def test_follow_then_rerun_loc_does_not_double():
    """따라잡기로 별지점·평단이 나간 뒤 /run loc 를 돌려도 또 내지 않는다"""
    eng, log, sent, _ = _follow_engine(cur=145.0)
    asyncio.run(eng._follow_ticker("SOXL", "20261109"))
    plan = [_loc(FillKind.GUARD_BUY, 3, 149.5), _loc(FillKind.STAR_BUY, 1, 175.93),
            _loc(FillKind.AVG_BUY, 2, 149.10)]
    out, _ = eng._drop_already_submitted("SOXL", "20261109", plan)
    assert out == []



def test_follow_does_not_churn_after_rejection():
    eng, log, sent, _ = _follow_engine(cur=145.0, reject=(175.93, 149.1))
    asyncio.run(eng._follow_ticker("SOXL", "20261109"))
    n = len(log)
    asyncio.run(eng._follow_ticker("SOXL", "20261109"))
    assert len(log) == n


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
