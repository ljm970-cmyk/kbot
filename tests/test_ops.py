"""
================================================================
운영 상태 · 긴급 정지 · 아침 요약 테스트

실행:  python tests/test_ops.py
================================================================
"""

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.ops import (  # noqa: E402
    health_report,
    mark_started,
    morning_brief,
    panic_stop,
    uptime_text,
)
from core.order_registry import OrderRegistry  # noqa: E402
from core.reconciler import CircuitBreaker  # noqa: E402
from core.state_manager import StateManager  # noqa: E402
from core.t_calculator import FillKind  # noqa: E402
from modes.base_mode import PlannedOrder  # noqa: E402

CFG = {"division": 20, "principal": 20000.0, "fee_rate": 0.0007, "is_active": True}


def _setup(halted=False):
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    reg = OrderRegistry(d / "o.db")
    sm.save_config("SOXL", CFG)
    st = sm.get_state("SOXL")
    st.T, st.holdings, st.avg_price, st.cash = 3.5, 24, 104.2, 17500.0
    st.last_eod_date = "20260918"
    if halted:
        CircuitBreaker.halt(st, "테스트 정지")
    sm.save_state(st)
    return sm, reg


class _Kiwoom:
    """예약주문 3건 중 2건만 봇이 낸 것"""

    def __init__(self, can_cancel=True, fail=()):
        self.token_expiry = datetime.now() + timedelta(hours=17)
        self.cancelled = []
        self._can_cancel = can_cancel
        self._fail = set(fail)

    def can_cancel_now(self):
        return self._can_cancel

    async def get_reserved_orders(self, ticker="", exchange="", include_cancelled=False):
        return [
            {"rsrv_ord_no": "9001", "rsrv_dt": "20260918", "stk_cd": "SOXL",
             "slby_tp": "매수", "ord_qty": "1", "ord_uv": "117.67"},
            {"rsrv_ord_no": "9002", "rsrv_dt": "20260918", "stk_cd": "SOXL",
             "slby_tp": "매수", "ord_qty": "1", "ord_uv": "111.11"},
            # 사용자가 직접 건 주문 — 원장에 없다
            {"rsrv_ord_no": "7777", "rsrv_dt": "20260918", "stk_cd": "SOXL",
             "slby_tp": "매도", "ord_qty": "5", "ord_uv": "130.00"},
        ]

    async def cancel_reserved(self, dt, no, ticker, ex):
        if no in self._fail:
            raise RuntimeError("취소 거부")
        self.cancelled.append(no)
        return {}


class _Config:
    class kiwoom:
        mock = False
    dry_run = True


def _record_bot_orders(reg, nos=("9001", "9002")):
    for i, no in enumerate(nos):
        rid = reg.record_submission("20260918", "SOXL", PlannedOrder(
            tag=FillKind.ENTRY_BUY if i == 0 else FillKind.CRASH_BUY,
            side="buy", trade_type="30", qty=1, price=117.67 - i))
        reg.attach_rsrv_ord_no(rid, no)


# ================================================================
# /panic — 봇 주문만 취소
# ================================================================

def test_panic_cancels_only_bot_orders():
    """계좌의 모든 예약주문을 취소하면 안 된다.
    사용자가 직접 건 주문은 원장에 없으므로 건드리지 않는다."""
    sm, reg = _setup()
    _record_bot_orders(reg)
    k = _Kiwoom()

    res = asyncio.run(panic_stop(k, sm, reg, lambda t: "NY"))

    assert k.cancelled == ["9001", "9002"]
    assert "7777" not in k.cancelled
    assert res.skipped_not_ours == 1
    assert len(res.cancelled) == 2


def test_panic_halts_all_tickers():
    sm, reg = _setup()
    _record_bot_orders(reg)
    asyncio.run(panic_stop(_Kiwoom(), sm, reg, lambda t: "NY"))
    assert CircuitBreaker.is_halted(sm.get_state("SOXL")) is True


def test_panic_halts_even_if_cancel_fails():
    """취소가 실패해도 신규 주문은 멈춰야 한다"""
    sm, reg = _setup()
    _record_bot_orders(reg)
    k = _Kiwoom(fail=("9001", "9002"))

    res = asyncio.run(panic_stop(k, sm, reg, lambda t: "NY"))

    assert CircuitBreaker.is_halted(sm.get_state("SOXL")) is True
    assert len(res.failed) == 2
    assert res.cancelled == []


def test_panic_warns_outside_cancel_window():
    """취소 가능 시간(08:00~22:25)이 아니면 알려야 한다"""
    sm, reg = _setup()
    _record_bot_orders(reg)
    res = asyncio.run(panic_stop(_Kiwoom(can_cancel=False), sm, reg, lambda t: "NY"))
    assert res.cancel_window_closed is True
    assert "08:00~22:25" in res.report()


def test_panic_marks_cancelled_in_registry():
    """취소한 주문은 원장에서도 빠져야 다음 매칭에 끼어들지 않는다"""
    sm, reg = _setup()
    _record_bot_orders(reg)
    asyncio.run(panic_stop(_Kiwoom(), sm, reg, lambda t: "NY"))
    assert reg.bot_reserved_orders("SOXL") == {}


def test_panic_report_lists_each_order():
    """급할 때 몇 건인지만 알면 나머지를 직접 처리할지 판단할 수 없다"""
    sm, reg = _setup()
    _record_bot_orders(reg)
    text = asyncio.run(panic_stop(_Kiwoom(), sm, reg, lambda t: "NY")).report()
    assert "117.67" in text
    assert "111.11" in text
    assert "봇이 낸 주문이 아님" in text


def test_bot_reserved_orders_excludes_dead():
    """취소·거부·만료된 건은 이미 살아있지 않다"""
    sm, reg = _setup()
    _record_bot_orders(reg, nos=("9001", "9002"))
    assert len(reg.bot_reserved_orders("SOXL")) == 2

    rec = reg.bot_reserved_orders("SOXL")["9001"]
    reg.set_status(rec.id, "rejected", "증거금 부족")
    assert len(reg.bot_reserved_orders("SOXL")) == 1


# ================================================================
# /health
# ================================================================

def test_health_shows_mode_and_state():
    sm, _ = _setup()
    mark_started()
    text = health_report(_Config(), _Kiwoom(), sm, None, None)
    assert "DRY RUN" in text
    assert "SOXL" in text
    assert "T=3.5000" in text
    assert "20260918" in text


def test_health_shows_halt_reason():
    sm, _ = _setup(halted=True)
    text = health_report(_Config(), _Kiwoom(), sm, None, None)
    assert "[정지]" in text
    assert "테스트 정지" in text


def test_health_survives_missing_websocket():
    sm, _ = _setup()
    text = health_report(_Config(), _Kiwoom(), sm, None, None)
    assert "미연결" in text


def test_health_shows_token_expiry():
    sm, _ = _setup()
    text = health_report(_Config(), _Kiwoom(), sm, None, None)
    assert "토큰" in text
    assert "남은" in text


def test_health_without_tickers():
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    text = health_report(_Config(), _Kiwoom(), sm, None, None)
    assert "설정된 종목 없음" in text


def test_uptime_text_format():
    mark_started()
    assert "분" in uptime_text()


# ================================================================
# 아침 요약
# ================================================================

def test_morning_brief_includes_yesterday_fills():
    """EOD 리포트는 05:30 에 와서 대개 읽히지 않는다.
    일어나서 볼 수 있게 어제 결과를 다시 정리한다."""
    sm, reg = _setup()
    reg.record_submission("20260918", "SOXL", PlannedOrder(
        tag=FillKind.ENTRY_BUY, side="buy", trade_type="30", qty=8, price=117.67))
    reg.record_fill("20260918", "SOXL", "buy", 8, 104.2, order_price=117.67)

    text = morning_brief(sm, reg, None)
    assert "SOXL" in text
    assert "매수 8주" in text
    assert "일정" in text


def test_morning_brief_when_no_fills():
    sm, reg = _setup()
    text = morning_brief(sm, reg, None)
    assert "어제 체결 없음" in text


def test_morning_brief_shows_halt():
    sm, reg = _setup(halted=True)
    text = morning_brief(sm, reg, None)
    assert "[정지]" in text


def test_morning_brief_without_tickers():
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    reg = OrderRegistry(d / "o.db")
    assert "설정된 종목이 없습니다" in morning_brief(sm, reg, None)


# ================================================================
# 배선
# ================================================================

def test_commands_registered():
    src = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    assert 'CommandHandler("health"' in src
    assert 'CommandHandler("panic"' in src
    assert 'pattern=r"^panic:"' in src


def test_panic_requires_confirmation():
    """되돌리기 어려운 동작이므로 확인을 받아야 한다"""
    src = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    block = src[src.index("async def _cmd_panic"):]
    block = block[:block.index("async def", 20)]
    assert "InlineKeyboardMarkup" in block
    assert "panic:go" in block


def test_morning_brief_scheduled():
    src = (ROOT / "scheduler" / "engine.py").read_text(encoding="utf-8")
    assert "BRIEF_HOUR" in src
    assert "_morning_brief" in src
    assert 'id="morning_brief"' in src


def test_uptime_marked_at_start():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "mark_started()" in src


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
