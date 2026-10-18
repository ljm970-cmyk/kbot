"""
================================================================
두 종목 달러 나눠 쓰기

종목을 차례로 접수하므로(SOXL → TQQQ), 앞 종목이 폭락대비까지 걸면
뒤 종목의 별지점·평단 매수가 빠질 수 있었다. 폭락대비는 거의 체결되지
않는 주문인데 달러를 가장 많이 묶는다.

  - 앞 종목의 폭락대비는 뒤 종목 핵심 매수 몫을 건드리지 못한다
  - 앞 종목이 실제로 묶은 금액만큼 뒤 종목 예산이 줄어든다
    (증권사 주문가능금액이 아직 반영하지 못했어도 겹쳐 쓰지 않는다)
  - 달러가 넉넉하면 예전과 똑같이 전부 접수한다
  - 한 종목만 운용하면 예전과 똑같다

실행:  python tests/test_cross_funding.py
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
PRICE = 146.25
DAY = date(2026, 9, 24)


def _o(tag, qty, price, side="buy"):
    return PlannedOrder(tag=tag, side=side, trade_type=TradeType.LOC, qty=qty, price=price)


# ================================================================
# 배분 함수
# ================================================================

def _buys():
    return ([_o(FillKind.STAR_BUY, 1, 175.93), _o(FillKind.AVG_BUY, 2, 149.10)]
            + [_o(FillKind.CRASH_BUY, 1, p) for p in (125.68, 100.55, 83.79)])


def test_reserve_blocks_crash_not_core():
    core = sum(order_cost(o, FEE) for o in _buys()[:2])
    a = allocate_buys(_buys(), core + 130.0, FEE, aux_reserve=500.0)
    assert a.core_complete
    assert not any(o.tag == FillKind.CRASH_BUY for o in a.placed)


def test_reserve_leaves_room_for_crash_above_it():
    core = sum(order_cost(o, FEE) for o in _buys()[:2])
    a = allocate_buys(_buys(), core + 500.0 + 130.0, FEE, aux_reserve=500.0)
    crash = [o.price for o in a.placed if o.tag == FillKind.CRASH_BUY]
    assert crash == [125.68]                 # 위에서부터 끊김 없이, 예약분은 남김


def test_zero_reserve_is_old_behaviour():
    core = sum(order_cost(o, FEE) for o in _buys()[:2])
    a = allocate_buys(_buys(), core + 130.0, FEE)
    assert [o.price for o in a.placed if o.tag == FillKind.CRASH_BUY] == [125.68]


# ================================================================
# 스케줄러 — 두 종목 실제 접수
# ================================================================

def _engine(cash, tickers=("SOXL", "TQQQ"), broker_deducts=True, halt=()):
    from core.reconciler import CircuitBreaker
    from scheduler.engine import SchedulerEngine
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    for t in tickers:
        sm.save_config(t, {"ticker": t, "division": 40, "principal": 20000.0,
                           "fee_rate": FEE, "is_active": True})
        st = sm.get_state(t)
        st.T, st.holdings, st.avg_price = 2.0, 6, 149.10
        st.next_core_need = COSTS[t][0] if COSTS else -1.0
        if t in halt:
            CircuitBreaker.halt(st, "테스트")
        sm.save_state(st)
    placed, sent, live = [], [], set()
    wallet = {"cash": cash}

    class K:
        async def available_usd(self, t, ex, price): return wallet["cash"]
        async def get_quote(self, t, ex): return {"cur_price": PRICE, "prev_close": PRICE}
        async def get_recent_closes(self, t, ex, n=5): return [PRICE] * 5
        async def get_open_orders(self, t, ex): return [{"ord_no": n} for n in live]

    class N:
        async def send(self, text): sent.append(text)

    class Cfg:
        dry_run = False

    class R:
        def __init__(self, n): self.ord_no = n

    async def _place(t, ex, order):
        placed.append((t, order))
        if broker_deducts and order.side == "buy":
            wallet["cash"] -= order_cost(order, FEE)
        no = f"N{len(placed)}"
        live.add(no)
        return R(no)

    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry = sm, OrderRegistry(d / "o.db")
    eng.kiwoom, eng.notifier, eng.config, eng._place = K(), N(), Cfg(), _place
    return eng, placed, sent


def _run(eng):
    from scheduler.engine import SubmitWindow
    asyncio.run(eng._submit(DAY, SubmitWindow.REGULAR, "LOC 접수"))


COSTS: dict = {}


def _actual_costs(t):
    """엔진이 실제로 내는 매수 주문의 (핵심 합계, 폭락대비 가격순 금액)"""
    eng, placed, _ = _engine(cash=1e9, tickers=(t,))
    _run(eng)
    buys = [o for _, o in placed if o.side == "buy"]
    core = round(sum(order_cost(o, FEE) for o in buys if o.tag != FillKind.CRASH_BUY), 2)
    crash = [order_cost(o, FEE) for o in sorted(
        (o for o in buys if o.tag == FillKind.CRASH_BUY), key=lambda o: -o.price)]
    return core, crash


COSTS.update({t: _actual_costs(t) for t in ("SOXL", "TQQQ")})
CORE_S, CRASH_S = COSTS["SOXL"]
CORE_Q, _ = COSTS["TQQQ"]
CORE2 = CORE_S + CORE_Q


def _tags(placed, t):
    return [o.tag for tt, o in placed if tt == t and o.side == "buy"]


def _is_core(tag):
    return tag != FillKind.CRASH_BUY


def test_second_ticker_core_survives_first_ticker_crash():
    """두 종목 핵심 + 폭락대비 한 칸 분량. 예전엔 SOXL 폭락대비가 먹고 TQQQ 핵심이 빠졌다"""
    eng, placed, sent = _engine(cash=CORE2 + CRASH_S[0] * 0.5)
    _run(eng)
    soxl, tqqq = _tags(placed, "SOXL"), _tags(placed, "TQQQ")
    assert FillKind.CRASH_BUY not in soxl
    assert FillKind.STAR_BUY in tqqq and FillKind.AVG_BUY in tqqq
    assert FillKind.STAR_BUY in soxl and FillKind.AVG_BUY in soxl
    assert any("[SOXL]" in m and "남겨뒀습니다" in m for m in sent)


def test_crash_goes_in_when_room_left_after_reserve():
    eng, placed, _ = _engine(cash=CORE2 + CRASH_S[0] + 1.0)
    _run(eng)
    soxl = _tags(placed, "SOXL")
    assert soxl.count(FillKind.CRASH_BUY) == 1
    assert all(t in _tags(placed, "TQQQ") for t in (FillKind.STAR_BUY, FillKind.AVG_BUY))


def test_rich_account_places_everything():
    eng, placed, _ = _engine(cash=100_000.0)
    _run(eng)
    for t in ("SOXL", "TQQQ"):
        assert _tags(placed, t).count(FillKind.CRASH_BUY) == len(COSTS[t][1])


def test_budget_prevents_double_spend_when_broker_lags():
    """증권사 주문가능금액이 앞 종목 주문을 아직 빼지 않았어도 합계가 넘지 않는다"""
    cash = CORE2 + 50.0
    eng, placed, _ = _engine(cash=cash, broker_deducts=False)
    _run(eng)
    spent = sum(order_cost(o, FEE) for _, o in placed if o.side == "buy")
    assert spent <= cash + 1e-6
    assert all(_is_core(o.tag) for _, o in placed if o.side == "buy")


def test_halted_later_ticker_reserves_nothing():
    eng, placed, _ = _engine(cash=CORE_S + CRASH_S[0] + 1.0, halt=("TQQQ",))
    _run(eng)
    assert FillKind.CRASH_BUY in _tags(placed, "SOXL")
    assert _tags(placed, "TQQQ") == []


def test_single_ticker_unchanged():
    eng, placed, _ = _engine(cash=CORE_S + CRASH_S[0] + 1.0, tickers=("SOXL",))
    _run(eng)
    assert _tags(placed, "SOXL").count(FillKind.CRASH_BUY) == 1


def test_shortfall_still_sells():
    eng, placed, _ = _engine(cash=10.0)
    _run(eng)
    assert any(o.side == "sell" for t, o in placed if t == "SOXL")
    assert any(o.side == "sell" for t, o in placed if t == "TQQQ")


def test_premarket_window_skips_budget_lookup():
    """프리장 창구는 매도만 — 계좌 조회를 하지 않는다"""
    from scheduler.engine import SubmitWindow
    eng, _, _ = _engine(cash=100.0)
    calls = []
    orig = eng._account_available

    async def spy():
        calls.append(1)
        return await orig()
    eng._account_available = spy
    asyncio.run(eng._submit(DAY, SubmitWindow.PRE_MARKET, "지정가매도"))
    assert calls == []


def test_second_ticker_lookup_failure_uses_budget():
    """뒤 종목 조회가 실패해도 예산 안에서 배분한다 (예전엔 배분 없이 전부 냈다)"""
    eng, placed, _ = _engine(cash=CORE2 + 50.0)
    orig = eng.kiwoom.available_usd
    calls = {"n": 0}

    async def flaky(t, ex, price):
        calls["n"] += 1
        if t == "TQQQ":
            raise RuntimeError("조회 실패")
        return await orig(t, ex, price)
    eng.kiwoom.available_usd = flaky
    _run(eng)
    tq = [o for t, o in placed if t == "TQQQ" and o.side == "buy"]
    assert tq and all(o.tag != FillKind.CRASH_BUY for o in tq)


def test_first_ticker_not_capped_by_budget_lookup():
    """예산 조회값이 첫 종목 자체 조회보다 조금 낮아도 첫 종목 핵심 매수는 빠지지 않는다"""
    eng, placed, _ = _engine(cash=CORE_S + 1.0, tickers=("SOXL", "TQQQ"))
    first = {"done": False}
    orig = eng._account_available

    async def low():
        await orig()
        return CORE_S - 5.0
    eng._account_available = low
    _run(eng)
    tags = _tags(placed, "SOXL")
    assert FillKind.STAR_BUY in tags and FillKind.AVG_BUY in tags


def test_reserve_error_does_not_block_sells():
    eng, placed, _ = _engine(cash=100_000.0)

    def boom(_):
        raise ValueError("깨진 상태")
    eng._core_reserve = boom
    _run(eng)
    assert any(o.side == "sell" for t, o in placed if t == "SOXL")
    assert any(o.side == "sell" for t, o in placed if t == "TQQQ")


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
