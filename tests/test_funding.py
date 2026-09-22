"""
================================================================
매일 부족분을 채우는 운용 — 자금 점검 테스트

원금은 장부상 금액으로 두고 실제 달러는 매일 필요한 만큼 채운다.
  - 부족해도 정지하지 않는다
  - 매도는 그대로 낸다
  - 매수만 건너뛰고, 입금 후 /run loc 로 매수만 다시 낼 수 있다

실행:  python tests/test_funding.py
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

from core.funding import FundingCheck, buy_need  # noqa: E402
from core.order_registry import OrderRegistry  # noqa: E402
from core.reconciler import CircuitBreaker  # noqa: E402
from core.state_manager import StateManager  # noqa: E402
from modes.base_mode import MarketSnapshot  # noqa: E402
from modes.normal_mode import NormalMode  # noqa: E402

CFG = {"division": 40, "principal": 20000.0, "fee_rate": 0.0007, "is_active": True}


# ================================================================
# FundingCheck
# ================================================================

def test_shortfall():
    c = FundingCheck(need=930.0, available=800.0)
    assert c.ok is False
    assert abs(c.shortfall - 130.0) < 1e-9
    assert "130.00 입금" in c.topup_text()


def test_enough():
    c = FundingCheck(need=930.0, available=3212.5)
    assert c.ok is True
    assert "충분" in c.topup_text()


def test_no_buys():
    assert "입금 불필요" in FundingCheck(need=0.0, available=0.0).topup_text()


def test_buy_need_includes_fee_and_margin():
    sm = StateManager(Path(tempfile.mkdtemp()) / "d")
    sm.save_config("SOXL", CFG)
    st = sm.get_state("SOXL")
    plan = NormalMode(st).plan(MarketSnapshot(prev_close=141.55, current_price=141.55))
    gross = sum(o.amount for o in plan.buys)
    need = buy_need(plan, st.fee_rate)
    assert need > gross
    assert need < gross * 1.01


def test_buy_need_zero_when_no_buys():
    class P:
        buys = []
    assert buy_need(P(), 0.0007) == 0.0


# ================================================================
# 스케줄러 — 부족하면 매수만 건너뛰고 매도는 낸다
# ================================================================

def _engine(available, holdings=0, avg=0.0, T=0.0):
    from scheduler.engine import SchedulerEngine, SubmitWindow  # noqa: F401

    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", CFG)
    st = sm.get_state("SOXL")
    st.holdings, st.avg_price, st.T = holdings, avg, T
    sm.save_state(st)
    reg = OrderRegistry(d / "o.db")

    placed, sent = [], []

    class K:
        async def available_usd(self, t, ex, price):
            return available

        async def get_quote(self, t, ex):
            return {"cur_price": 141.55, "prev_close": 141.55}

        async def get_recent_closes(self, t, ex, n=5):
            return [141.55] * 5

    class N:
        async def send(self, text):
            sent.append(text)

    class Cfg:
        dry_run = False

    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry, eng.kiwoom = sm, reg, K()
    eng.notifier, eng.config = N(), Cfg()

    class R:
        rsrv_ord_no = "R1"

    async def _place(t, ex, order):
        placed.append(order)
        return R()

    eng._place = _place
    return eng, placed, sent, sm


def test_short_funds_skip_buys_keep_sells():
    """보유 중인 상태에서 달러가 모자라면 매수만 빠지고 쿼터매도는 나간다"""
    from scheduler.engine import SubmitWindow
    eng, placed, sent, sm = _engine(available=10.0, holdings=40, avg=140.0, T=8.0)
    asyncio.run(eng._submit_ticker("SOXL", "20260922", SubmitWindow.REGULAR, "LOC 예약"))

    assert placed, "매도까지 막혔다"
    assert all(o.side == "sell" for o in placed)
    assert any("매수를 건너뜁니다" in s for s in sent)
    assert any("/run loc" in s for s in sent)


def test_short_funds_do_not_halt():
    from scheduler.engine import SubmitWindow
    eng, placed, sent, sm = _engine(available=10.0, holdings=40, avg=140.0, T=8.0)
    asyncio.run(eng._submit_ticker("SOXL", "20260922", SubmitWindow.REGULAR, "LOC 예약"))
    assert CircuitBreaker.is_halted(sm.get_state("SOXL")) is False


def test_enough_funds_submit_everything():
    from scheduler.engine import SubmitWindow
    eng, placed, sent, sm = _engine(available=100000.0, holdings=40, avg=140.0, T=8.0)
    asyncio.run(eng._submit_ticker("SOXL", "20260922", SubmitWindow.REGULAR, "LOC 예약"))
    assert any(o.side == "buy" for o in placed)
    assert any(o.side == "sell" for o in placed)


def test_rerun_after_deposit_submits_only_buys():
    """입금 후 /run loc — 이미 낸 매도는 빼고 매수만 접수돼야 한다"""
    from scheduler.engine import SubmitWindow
    eng, placed, sent, sm = _engine(available=10.0, holdings=40, avg=140.0, T=8.0)
    asyncio.run(eng._submit_ticker("SOXL", "20260922", SubmitWindow.REGULAR, "LOC 예약"))
    first_sells = list(placed)

    placed.clear()

    async def rich(t, ex, price):
        return 100000.0
    eng.kiwoom.available_usd = rich

    asyncio.run(eng._submit_ticker("SOXL", "20260922", SubmitWindow.REGULAR, "LOC 예약"))
    assert placed, "입금 후에도 매수가 안 나갔다"
    assert all(o.side == "buy" for o in placed), "매도가 중복으로 나갔다"
    assert first_sells


def test_first_entry_with_no_holdings_and_short_funds():
    """보유 0 + 달러 부족이면 낼 주문이 없다. 알리고 끝낸다."""
    from scheduler.engine import SubmitWindow
    eng, placed, sent, sm = _engine(available=10.0)
    asyncio.run(eng._submit_ticker("SOXL", "20260922", SubmitWindow.REGULAR, "LOC 예약"))
    assert placed == []
    assert any("입금" in s for s in sent)


# ================================================================
# EOD 가 다음 거래일 필요 금액을 남긴다
# ================================================================

def test_eod_stores_next_buy_need():
    from eod.calculator import EndOfDayCalculator
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", CFG)
    st = sm.get_state("SOXL")
    eod = EndOfDayCalculator(OrderRegistry(d / "o.db"))
    eod.run(st, "20260922", [], 141.55,
            MarketSnapshot(prev_close=141.55, current_price=141.55))
    assert st.next_buy_need > 0


def test_next_buy_need_survives_restart():
    from modes.base_mode import PositionState
    sm = StateManager(Path(tempfile.mkdtemp()) / "d")
    sm.save_config("SOXL", CFG)
    st = sm.get_state("SOXL")
    st.next_buy_need = 930.12
    assert PositionState.from_dict(st.to_dict()).next_buy_need == 930.12


# ================================================================
# 알림 · 배선
# ================================================================

def test_morning_brief_shows_topup():
    from core.ops import morning_brief
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", CFG)
    st = sm.get_state("SOXL")
    st.next_buy_need = 930.0
    sm.save_state(st)
    text = morning_brief(sm, OrderRegistry(d / "o.db"), None, {"SOXL": 800.0})
    assert "130.00 입금" in text


def test_funding_reminder_scheduled_before_orders():
    src = (ROOT / "scheduler" / "engine.py").read_text(encoding="utf-8")
    assert '"funding_reminder", sched.premarket - _td(hours=1)' in src
    assert "async def _funding_reminder(self, session=None)" in src


def test_buying_power_uses_no_margin_amount():
    """증거금 기준 금액에는 미수가 섞인다. 미수불가 금액을 써야 한다."""
    src = (ROOT / "kiwoom" / "api_client.py").read_text(encoding="utf-8")
    block = src[src.index("async def get_buying_power"):]
    block = block[:block.index("async def", 20)]
    assert "min_ord_alowa" in block
    assert "ust31490" in block


def test_buying_power_query_is_retryable():
    """조회는 재시도해도 안전하다 (주문이 아니다)"""
    from kiwoom.api_client import NON_IDEMPOTENT_APIS
    assert "ust31490" not in NON_IDEMPOTENT_APIS


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
