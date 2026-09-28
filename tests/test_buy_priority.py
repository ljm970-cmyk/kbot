"""
================================================================
매수 우선순위 테스트 — 달러가 모자랄 때 무엇을 먼저 넣나

원본은 폭락대비까지 합친 금액이 모자라면 매수를 통째로 건너뛰었다.
폭락대비는 거의 체결되지 않는데 예수금을 가장 많이 묶어서, 정작
별지점·평단 매수가 빠지고 방법론이 그날 멈췄다.

  1순위  핵심 매수 (별지점·평단·처음매수 등, T 를 움직임) — 가격 높은 것부터
  2순위  폭락대비 (T 불변) — 핵심이 모두 들어갔을 때만, 가격 높은 것부터

실행:  python tests/test_buy_priority.py
================================================================
"""

import asyncio
import sys
import tempfile
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


def _o(tag, qty, price, side="buy"):
    return PlannedOrder(tag=tag, side=side, trade_type=TradeType.LOC, qty=qty, price=price)


# 2026-09-24 실제 계획 (T=2, 평단 149.10)
STAR = _o(FillKind.STAR_BUY, 1, 175.93)
AVG = _o(FillKind.AVG_BUY, 2, 149.10)
CRASH = [_o(FillKind.CRASH_BUY, 1, p) for p in (125.68, 100.55, 83.79, 71.82, 62.84)]
PLAN = [STAR, AVG] + CRASH
CORE_COST = order_cost(STAR, FEE) + order_cost(AVG, FEE)


# ================================================================
# 배분
# ================================================================

def test_everything_fits():
    a = allocate_buys(PLAN, 10_000.0, FEE)
    assert a.all_placed and a.core_complete
    assert len(a.placed) == 7


def test_only_core_fits():
    """별지점·평단만 살 돈이면 그 두 건만 들어간다"""
    a = allocate_buys(PLAN, CORE_COST + 1.0, FEE)
    assert a.placed == [STAR, AVG]
    assert a.core_complete
    assert set(map(id, a.skipped)) == set(map(id, CRASH))


def test_crash_filled_from_highest_price():
    """폭락대비는 체결 가능성이 큰 높은 가격부터"""
    a = allocate_buys(PLAN, CORE_COST + order_cost(CRASH[0], FEE)
                      + order_cost(CRASH[1], FEE) + 1.0, FEE)
    assert a.placed == [STAR, AVG, CRASH[0], CRASH[1]]


def test_crash_ladder_has_no_gaps():
    """위 칸이 안 들어가면 아래 칸도 넣지 않는다 — 사다리가 뒤집히지 않게"""
    budget = CORE_COST + order_cost(CRASH[1], FEE) + 1.0   # 125.68 은 모자라고 100.55 는 됨
    assert budget < CORE_COST + order_cost(CRASH[0], FEE)
    a = allocate_buys(PLAN, budget, FEE)
    assert a.placed == [STAR, AVG]


def test_star_before_avg_when_short():
    """핵심 중에서도 가격이 높은(체결 가능성이 큰) 별지점부터"""
    a = allocate_buys(PLAN, order_cost(STAR, FEE) + 1.0, FEE)
    assert a.placed == [STAR]
    assert not a.core_complete


def test_crash_not_placed_when_core_incomplete():
    """핵심이 빠진 날 폭락대비만 걸면 T 는 그대로인 채 물량만 는다"""
    # 별지점만 들어가고 평단은 못 들어갈 돈 — 남는 돈으로 폭락대비 1건은 되지만 넣지 않는다
    budget = order_cost(STAR, FEE) + order_cost(CRASH[0], FEE) + 1.0
    assert budget < CORE_COST
    a = allocate_buys(PLAN, budget, FEE)
    assert a.placed == [STAR]
    assert all(o.tag == FillKind.CRASH_BUY or o is AVG for o in a.skipped)


def test_cheaper_core_tried_after_expensive_one_skipped():
    """비싼 핵심 한 건이 안 들어가도 다음 건은 시도한다"""
    big = _o(FillKind.STAR_BUY, 10, 175.0)      # $1,750
    small = _o(FillKind.AVG_BUY, 2, 149.0)       # $298
    a = allocate_buys([big, small], 400.0, FEE)
    assert a.placed == [small]


def test_nothing_fits():
    a = allocate_buys(PLAN, 10.0, FEE)
    assert a.placed == []
    assert not a.core_complete


def test_quantities_never_split():
    """수량을 쪼개면 T 반영(+1/+0.5)의 뜻이 달라진다"""
    a = allocate_buys(PLAN, CORE_COST - 1.0, FEE)
    for o in a.placed:
        assert o.qty in (1, 2)


def test_reverse_quarter_buy_is_core():
    rq = _o(FillKind.REVERSE_QUARTER_BUY, 3, 50.0)
    cr = _o(FillKind.CRASH_BUY, 1, 45.0)
    a = allocate_buys([cr, rq], order_cost(rq, FEE) + 1.0, FEE)
    assert a.placed == [rq]


def test_entry_buy_is_core():
    eb = _o(FillKind.ENTRY_BUY, 3, 163.11)
    cr = _o(FillKind.CRASH_BUY, 1, 125.0)
    a = allocate_buys([cr, eb], order_cost(eb, FEE) + 1.0, FEE)
    assert a.placed == [eb]


def test_needs_reported():
    a = allocate_buys(PLAN, 0.0, FEE)
    assert abs(a.core_need - round(CORE_COST, 2)) < 0.01
    assert a.full_need > a.core_need


# ================================================================
# 입금 안내 문구
# ================================================================

def test_topup_core_ok_crash_partial():
    t = FundingCheck(need=924.05, available=600.0, core_need=478.10).topup_text()
    assert "별지점·평단 매수 $478.10 는 충분" in t
    assert "폭락대비는 일부만" in t


def test_topup_core_short():
    t = FundingCheck(need=924.05, available=300.0, core_need=478.10).topup_text()
    assert "별지점·평단 매수에도 $178.10 부족" in t
    assert "최소 $178.10" in t and "전체 $624.05" in t


def test_topup_without_core_info_unchanged():
    t = FundingCheck(need=924.05, available=800.0).topup_text()
    assert "$124.05 입금이 필요" in t


def test_topup_enough():
    assert "충분" in FundingCheck(need=924.05, available=5000.0, core_need=478.10).topup_text()


# ================================================================
# EOD — 핵심 금액 저장
# ================================================================

def test_eod_stores_core_need():
    from eod.calculator import EndOfDayCalculator
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", {"division": 40, "principal": 20000.0,
                            "fee_rate": FEE, "is_active": True})
    st = sm.get_state("SOXL")
    st.T, st.holdings, st.avg_price, st.cash = 2.0, 6, 149.10, 19104.77
    EndOfDayCalculator(OrderRegistry(d / "o.db")).run(
        st, "20260923", [], 146.25, MarketSnapshot(prev_close=146.25, current_price=146.25))
    assert 0 < st.next_core_need < st.next_buy_need


def test_core_need_matches_plan():
    st = PositionState(ticker="SOXL", division=40, principal=20000.0, fee_rate=FEE,
                       T=2.0, holdings=6, avg_price=149.10, cash=19104.77)
    plan = NormalMode(st).plan(MarketSnapshot(prev_close=146.25, current_price=146.25))
    core = core_buy_need(plan, FEE)
    expect = sum(order_cost(o, FEE) for o in plan.buys if o.tag != FillKind.CRASH_BUY)
    assert abs(core - round(expect, 2)) < 0.01


def test_unknown_core_need_defaults_to_minus_one():
    """이전 버전 상태 파일에는 필드가 없다. 0 으로 읽으면 '핵심 매수 없음' 이 된다."""
    assert PositionState.from_dict({"ticker": "SOXL", "division": 40,
                                    "principal": 20000.0}).next_core_need == -1.0


# ================================================================
# 스케줄러 — 실제 접수
# ================================================================

def _engine(available, T=2.0, holdings=6, avg=149.10):
    from scheduler.engine import SchedulerEngine
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", {"division": 40, "principal": 20000.0,
                            "fee_rate": FEE, "is_active": True})
    st = sm.get_state("SOXL")
    st.T, st.holdings, st.avg_price = T, holdings, avg
    sm.save_state(st)
    placed, sent = [], []
    live = set()    # 증권사 미체결 — 접수한 주문이 그대로 살아 있다

    class K:
        async def available_usd(self, t, ex, price): return available
        async def get_quote(self, t, ex): return {"cur_price": 146.25, "prev_close": 146.25}
        async def get_recent_closes(self, t, ex, n=5): return [146.25] * 5
        async def get_open_orders(self, t, ex): return [{"ord_no": n} for n in live]

    class N:
        async def send(self, text): sent.append(text)

    class Cfg:
        dry_run = False

    class R:
        def __init__(self, n): self.ord_no = n

    async def _place(t, ex, order):
        placed.append(order)
        no = f"N{len(placed)}-{id(order)}"
        live.add(no)
        return R(no)

    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry = sm, OrderRegistry(d / "o.db")
    eng.kiwoom, eng.notifier, eng.config, eng._place = K(), N(), Cfg(), _place
    return eng, placed, sent


def _run(eng):
    from scheduler.engine import SubmitWindow
    asyncio.run(eng._submit_ticker("SOXL", "20260924", SubmitWindow.REGULAR, "LOC 접수"))


def test_engine_places_core_when_crash_does_not_fit():
    eng, placed, sent = _engine(available=500.0)
    _run(eng)
    tags = [o.tag for o in placed if o.side == "buy"]
    assert FillKind.STAR_BUY in tags and FillKind.AVG_BUY in tags
    assert FillKind.CRASH_BUY not in tags
    assert any("일부만 접수" in s and "폭락대비만 일부 빠짐" in s for s in sent)


def test_engine_sells_kept_when_buys_trimmed():
    eng, placed, _ = _engine(available=500.0)
    _run(eng)
    assert any(o.side == "sell" for o in placed)


def test_engine_rerun_after_deposit_adds_only_missing():
    """입금 후 /run loc — 이미 낸 별지점·평단은 다시 내지 않고 빠진 것만"""
    eng, placed, _ = _engine(available=500.0)
    _run(eng)
    first = [o.tag for o in placed]

    async def rich(t, ex, price): return 100_000.0
    eng.kiwoom.available_usd = rich
    placed.clear()
    _run(eng)
    again = [o.tag for o in placed]
    assert FillKind.STAR_BUY not in again and FillKind.AVG_BUY not in again
    assert FillKind.CRASH_BUY in again
    assert FillKind.STAR_BUY in first


def test_engine_all_fit_is_quiet_about_funding():
    eng, _, sent = _engine(available=100_000.0)
    _run(eng)
    assert not any("달러가 부족" in s for s in sent)


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
