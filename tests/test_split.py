"""
================================================================
액면분할·병합 테스트

SOXL 은 2023년 1:10 병합 이력이 있다. 분할·병합은 수량과 단가가 반대로
움직이고 **보유원가는 보존된다**. 이걸 체결 누락으로 보면
  - "체결 누락이나 수동 거래" 라는 엉뚱한 안내가 나가고
  - 옛 가격으로 걸린 주문이 그대로 남아 계획에 없던 매매가 일어난다

실행:  python tests/test_split.py
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

from core.order_registry import OrderRegistry  # noqa: E402
from core.reconciler import (  # noqa: E402
    CircuitBreaker,
    Severity,
    apply_reconcile,
    detect_split,
    reconcile,
)
from core.state_manager import StateManager  # noqa: E402
from core.t_calculator import FillKind  # noqa: E402
from kiwoom.constants import TradeType  # noqa: E402
from modes.base_mode import PlannedOrder, PositionState  # noqa: E402


def _state(qty=60, avg=10.0, T=8.0, cash=19000.0):
    return PositionState(ticker="SOXL", division=40, principal=20000.0,
                         fee_rate=0.0007, T=T, holdings=qty,
                         avg_price=avg, cash=cash)


def _broker(qty, avg):
    return {"poss_qty": qty, "avg_price": avg}


# ================================================================
# 판정
# ================================================================

def test_reverse_split_detected():
    s = detect_split(60, 10.0, 6, 100.0)
    assert s is not None and s.name == "병합" and s.text == "10:1"


def test_forward_split_detected():
    s = detect_split(6, 149.10, 12, 74.55)
    assert s is not None and s.name == "분할" and s.text == "1:2"


def test_fractional_share_cashout_still_detected():
    """1:10 병합에서 63주 → 6주. 단주 0.3은 현금청산된다."""
    assert detect_split(63, 10.0, 6, 100.0) is not None


def test_missed_fill_is_not_split():
    """체결 누락은 수량과 원가가 같이 늘어난다"""
    assert detect_split(6, 149.10, 7, 148.0) is None


def test_doubled_position_with_same_price_is_not_split():
    """수량만 2배인데 단가가 그대로면 분할이 아니라 중복 매수다"""
    assert detect_split(6, 149.10, 12, 149.10) is None


def test_matching_position_is_not_split():
    assert detect_split(6, 149.10, 6, 149.10) is None


def test_odd_ratio_is_not_split():
    assert detect_split(60, 10.0, 41, 14.63) is None


# ================================================================
# 대조 — 정지하지 않고 넘어간다
# ================================================================

def test_split_warns_but_does_not_halt():
    st = _state()
    r = reconcile(st, _broker(6, 100.0))
    assert r.should_halt is False
    assert r.severity == Severity.WARNING
    assert any(f.code == "split_detected" for f in r.findings)
    assert "병합" in r.report()


def test_split_keeps_t_and_cash():
    """수량·평단만 맞추고 T와 잔금은 그대로 둔다"""
    st = _state(T=8.0, cash=19000.0)
    r = reconcile(st, _broker(6, 100.0))
    apply_reconcile(st, r, _broker(6, 100.0))

    assert st.holdings == 6
    assert st.avg_price == 100.0
    assert st.T == 8.0
    assert st.cash == 19000.0
    assert CircuitBreaker.is_halted(st) is False


def test_split_preserves_holding_cost():
    st = _state(qty=60, avg=10.0)
    before = st.holdings * st.avg_price
    r = reconcile(st, _broker(6, 100.0))
    apply_reconcile(st, r, _broker(6, 100.0))
    assert abs(st.holdings * st.avg_price - before) < 1e-6


def test_position_wiped_halts():
    """보유가 0이 되면 정지한다.

    병합 단주청산인지 수동 전량매도인지 봇은 구별할 수 없다.
    어느 쪽이든 사람이 확인해야 하므로 일반 불일치로 멈춘다.
    """
    st = _state(qty=6, avg=100.0)
    r = reconcile(st, _broker(0, 0.0))
    assert r.should_halt is True
    assert any(f.code == "holdings_mismatch" for f in r.findings)


def test_split_skips_avg_deviation_check():
    """분할이면 평단이 배수로 바뀌는 게 정상 — 괴리로 정지하면 안 된다"""
    st = _state(qty=60, avg=10.0)
    r = reconcile(st, _broker(6, 100.0))
    assert not any(f.code.startswith("avg_") for f in r.findings)
    assert r.should_halt is False


def test_real_mismatch_still_halts():
    """분할이 아닌 불일치는 여전히 정지한다"""
    st = _state(qty=6, avg=149.10)
    r = reconcile(st, _broker(9, 149.10))
    assert r.should_halt is True
    assert any(f.code == "holdings_mismatch" for f in r.findings)


# ================================================================
# 옛 가격 주문 취소
# ================================================================

def _engine(live_nos, fail=()):
    from scheduler.engine import SchedulerEngine
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", {"division": 40, "principal": 20000.0,
                            "fee_rate": 0.0007, "is_active": True})
    reg = OrderRegistry(d / "o.db")
    sent, cancelled = [], []

    class K:
        async def get_open_orders(self, t, ex):
            return [{"ord_no": n} for n in live_nos]

        async def cancel(self, no, t, ex):
            if no in fail:
                raise RuntimeError("취소 거부")
            cancelled.append(no)
            return {}

    class N:
        async def send(self, text): sent.append(text)

    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry, eng.kiwoom, eng.notifier = sm, reg, K(), N()
    return eng, reg, sent, cancelled


def _record(reg, no, tag=FillKind.AVG_BUY, side="buy", qty=2, price=149.10):
    rid = reg.record_submission("20260924", "SOXL", PlannedOrder(
        tag=tag, side=side, trade_type=TradeType.LOC, qty=qty, price=price))
    reg.attach_ord_no(rid, no)
    return rid


def test_stale_orders_cancelled_after_split():
    eng, reg, sent, cancelled = _engine(live_nos=["A1", "A2"])
    _record(reg, "A1")
    _record(reg, "A2", tag=FillKind.STAR_BUY, qty=1, price=175.93)

    asyncio.run(eng._cancel_stale_orders("SOXL", "NY"))
    assert cancelled == ["A1", "A2"]
    assert reg.bot_live_orders("SOXL") == {}
    assert any("2건을 취소" in s for s in sent)


def test_personal_orders_untouched_after_split():
    eng, reg, sent, cancelled = _engine(live_nos=["A1", "USER9"])
    _record(reg, "A1")
    asyncio.run(eng._cancel_stale_orders("SOXL", "NY"))
    assert cancelled == ["A1"]


def test_cancel_failure_is_reported():
    eng, reg, sent, cancelled = _engine(live_nos=["A1"], fail={"A1"})
    _record(reg, "A1")
    asyncio.run(eng._cancel_stale_orders("SOXL", "NY"))
    assert any("직접 취소" in s for s in sent)


def test_nothing_to_cancel_is_quiet():
    eng, reg, sent, _ = _engine(live_nos=[])
    asyncio.run(eng._cancel_stale_orders("SOXL", "NY"))
    assert sent == []


def test_reconcile_triggers_cancel():
    src = (ROOT / "scheduler" / "engine.py").read_text(encoding="utf-8")
    assert 'f.code == "split_detected"' in src
    assert "_cancel_stale_orders(ticker, exchange)" in src


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
