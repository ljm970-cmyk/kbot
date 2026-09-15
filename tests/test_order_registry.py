"""
================================================================
주문 원장 테스트 — 태그 복원이 정확한지 고정

실행:  python tests/test_order_registry.py
================================================================
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.order_registry import OrderRegistry, OrderStatus
from core.t_calculator import FillKind, TCalculator
from kiwoom.constants import TradeType
from modes.base_mode import MarketSnapshot, PositionState
from modes.normal_mode import NormalMode

DAY = "20260915"
TICKER = "TQQQ"


def _reg() -> OrderRegistry:
    tmp = tempfile.mkdtemp()
    return OrderRegistry(Path(tmp) / "orders.db")


def _plan():
    """전반전 계획: 별지점 3주 @76.02, 평단 4주 @69.75, 폭락대비 5건"""
    st = PositionState(ticker=TICKER, division=40, principal=20000, fee_rate=0.0007,
                       T=8.0, avg_price=69.75, holdings=60, cash=539.23 * 32)
    return NormalMode(st).plan(MarketSnapshot(prev_close=78.0, current_price=76.0))


# ================================================================
# 기록
# ================================================================

def test_record_plan_stores_every_tag():
    reg = _reg()
    plan = _plan()
    ids = reg.record_plan(DAY, TICKER, plan.orders)
    assert len(ids) == len(plan.orders)

    rows = reg.day_orders(DAY, TICKER)
    tags = [r.tag for r in rows]
    assert FillKind.STAR_BUY in tags
    assert FillKind.AVG_BUY in tags
    assert tags.count(FillKind.CRASH_BUY) == 5
    assert FillKind.QUARTER_SELL in tags
    assert FillKind.TARGET_SELL in tags


def test_attach_numbers():
    reg = _reg()
    plan = _plan()
    rid = reg.record_submission(DAY, TICKER, plan.orders[0], rsrv_ord_no="000000000221")
    reg.attach_ord_no(rid, "000000282")
    rec = reg.get(rid)
    assert rec.ord_no == "000000282"
    assert rec.rsrv_ord_no == "000000000221"


# ================================================================
# 매칭 — ord_no 경로
# ================================================================

def test_resolve_by_ord_no():
    reg = _reg()
    plan = _plan()
    rid = reg.record_submission(DAY, TICKER, plan.orders[0], ord_no="000000282")
    rec = reg.resolve(DAY, TICKER, "buy", price=None, ord_no="000000282")
    assert rec.id == rid
    assert rec.tag == FillKind.STAR_BUY


# ================================================================
# 매칭 — 예약주문 경로 (ord_no 를 모른다)
# ================================================================

def test_resolve_by_price_distinguishes_star_and_avg():
    """예약주문은 ord_no 를 알 수 없으므로 단가로 갈라야 한다"""
    reg = _reg()
    plan = _plan()
    reg.record_plan(DAY, TICKER, plan.orders)

    star = reg.resolve(DAY, TICKER, "buy", price=76.02)
    avg = reg.resolve(DAY, TICKER, "buy", price=69.75)
    assert star.tag == FillKind.STAR_BUY
    assert avg.tag == FillKind.AVG_BUY


def test_resolve_picks_correct_crash_level():
    """폭락대비 5건은 모두 1주라 단가로만 갈린다"""
    reg = _reg()
    plan = _plan()
    reg.record_plan(DAY, TICKER, plan.orders)

    rec = reg.resolve(DAY, TICKER, "buy", price=53.92)   # 539.23/10
    assert rec.tag == FillKind.CRASH_BUY
    assert rec.qty == 1


def test_all_buy_prices_are_distinct():
    """단가 매칭이 성립하려면 하루 매수 주문의 단가가 모두 달라야 한다"""
    plan = _plan()
    prices = [o.price for o in plan.buys]
    assert len(prices) == len(set(prices))


def test_resolve_moc_without_price():
    """MOC 는 단가가 없다. 단가 없는 미체결 주문이 하나면 그것으로 확정."""
    reg = _reg()

    class _O:
        tag = FillKind.REVERSE_MOC_SELL
        side = "sell"
        trade_type = TradeType.MOC
        qty = 10
        price = None
        note = ""

    reg.record_submission(DAY, TICKER, _O())
    rec = reg.resolve(DAY, TICKER, "sell", price=None, qty=10)
    assert rec.tag == FillKind.REVERSE_MOC_SELL


def test_rejected_order_is_not_matched():
    """ust21205 검증에서 거부된 주문은 체결될 수 없으므로 후보에서 빠진다"""
    reg = _reg()
    plan = _plan()
    rid = reg.record_submission(DAY, TICKER, plan.orders[0], rsrv_ord_no="R1")
    reg.mark_rejected_by_rsrv_no("R1", "증거금 부족")

    assert reg.get(rid).status == OrderStatus.REJECTED
    assert reg.resolve(DAY, TICKER, "buy", price=plan.orders[0].price) is None


def test_trade_type_disambiguates_price_collision():
    """T=0 에서 TQQQ 별%가 15% 라 쿼터매도(LOC)와 목표매도(지정가)의
    단가가 겹친다. 매매유형으로 갈려야 한다."""
    reg = _reg()
    st = PositionState(ticker=TICKER, division=40, principal=20000, fee_rate=0.0007,
                       T=0.0, avg_price=69.75, holdings=60, cash=20000)
    plan = NormalMode(st).plan(MarketSnapshot(prev_close=78.0, current_price=76.0))
    reg.record_plan(DAY, TICKER, plan.orders)

    quarter = next(o for o in plan.orders if o.tag == FillKind.QUARTER_SELL)
    target = next(o for o in plan.orders if o.tag == FillKind.TARGET_SELL)
    assert quarter.price == target.price          # 실제로 겹친다
    assert quarter.trade_type != target.trade_type

    q = reg.resolve(DAY, TICKER, "sell", price=quarter.price,
                    qty=quarter.qty, trade_type=TradeType.LOC)
    t = reg.resolve(DAY, TICKER, "sell", price=target.price,
                    qty=target.qty, trade_type=TradeType.LIMIT)
    assert q.tag == FillKind.QUARTER_SELL
    assert t.tag == FillKind.TARGET_SELL


def test_plan_warns_on_same_type_price_collision():
    """같은 매매유형끼리 단가가 겹치면 계획 단계에서 경고한다"""
    st = PositionState(ticker=TICKER, division=40, principal=20000, fee_rate=0.0007,
                       T=0.0, avg_price=69.75, holdings=60, cash=20000)
    plan = NormalMode(st).plan(MarketSnapshot(prev_close=78.0, current_price=76.0))
    # 매매유형이 다르므로 경고가 없어야 한다
    assert not any("단가 충돌" in w for w in plan.warnings)


# ================================================================
# 중복 접수 방지
# ================================================================

def test_already_submitted_detects_duplicates():
    """같은 날 같은 주문을 두 번 내면 포지션이 두 배가 된다"""
    reg = _reg()
    plan = _plan()
    tags = [o.tag for o in plan.orders]
    assert reg.already_submitted(DAY, TICKER, tags) == []

    reg.record_plan(DAY, TICKER, plan.orders)
    dup = reg.already_submitted(DAY, TICKER, tags)
    assert FillKind.STAR_BUY in dup
    assert FillKind.TARGET_SELL in dup


def test_already_submitted_is_per_day():
    reg = _reg()
    reg.record_plan(DAY, TICKER, _plan().orders)
    assert reg.already_submitted("20260916", TICKER, [FillKind.STAR_BUY]) == []


def test_rejected_orders_can_be_resubmitted():
    """거부된 주문은 다시 내야 하므로 중복으로 보지 않는다"""
    reg = _reg()
    plan = _plan()
    rid = reg.record_submission(DAY, TICKER, plan.orders[0], rsrv_ord_no="R9")
    reg.mark_rejected_by_rsrv_no("R9", "증거금 부족")
    assert reg.already_submitted(DAY, TICKER, [plan.orders[0].tag]) == []


def test_expired_orders_still_count_as_submitted():
    """만료는 '냈지만 체결 안 됨' 이다. 같은 날 다시 내면 중복이다."""
    reg = _reg()
    plan = _plan()
    reg.record_plan(DAY, TICKER, plan.orders)
    reg.expire_open_orders(DAY, TICKER)
    assert FillKind.STAR_BUY in reg.already_submitted(DAY, TICKER, [FillKind.STAR_BUY])


def test_scheduler_checks_duplicates():
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parent.parent / "scheduler" / "engine.py").read_text()
    assert "already_submitted" in src
    assert src.index("already_submitted") < src.index("await self._place(")


# ================================================================
# 체결 반영
# ================================================================

def test_record_fill_marks_filled():
    reg = _reg()
    plan = _plan()
    reg.record_plan(DAY, TICKER, plan.orders)

    # LOC 은 주문단가와 체결단가가 다르다 (종가로 체결)
    rec = reg.record_fill(DAY, TICKER, "buy", fill_qty=3, fill_price=68.10,
                          order_price=76.02, ord_no="000000301")
    assert rec.tag == FillKind.STAR_BUY
    assert rec.status == OrderStatus.FILLED
    assert reg.get(rec.id).ord_no == "000000301"


def test_partial_fill():
    reg = _reg()
    plan = _plan()
    reg.record_plan(DAY, TICKER, plan.orders)

    r1 = reg.record_fill(DAY, TICKER, "buy", 2, 68.10, order_price=76.02)
    assert r1.status == OrderStatus.PARTIAL
    r2 = reg.record_fill(DAY, TICKER, "buy", 1, 68.10, order_price=76.02)
    assert r2.status == OrderStatus.FILLED


def test_unmatched_fill_returns_none():
    reg = _reg()
    reg.record_plan(DAY, TICKER, _plan().orders)
    assert reg.record_fill(DAY, TICKER, "buy", 5, 10.0, order_price=10.0) is None


# ================================================================
# DayFills 생성 → T 계산
# ================================================================

def test_build_day_fills_full_buy():
    """별지점 + 평단 둘 다 체결 → 1회 매수 → T+1"""
    reg = _reg()
    plan = _plan()
    reg.record_plan(DAY, TICKER, plan.orders)
    reg.record_fill(DAY, TICKER, "buy", 3, 68.10, order_price=76.02)
    reg.record_fill(DAY, TICKER, "buy", 4, 68.10, order_price=69.75)

    fills = reg.build_day_fills(DAY, TICKER, holdings_after=67)
    assert fills.kinds == {FillKind.STAR_BUY, FillKind.AVG_BUY}

    c = TCalculator(40, t=8.0)
    assert c.apply_normal_day(fills).t_after == 9.0


def test_build_day_fills_half_buy():
    """별지점만 체결 → 절반 매수 → T+0.5"""
    reg = _reg()
    plan = _plan()
    reg.record_plan(DAY, TICKER, plan.orders)
    reg.record_fill(DAY, TICKER, "buy", 3, 75.00, order_price=76.02)

    fills = reg.build_day_fills(DAY, TICKER, holdings_after=63)
    c = TCalculator(40, t=8.0)
    assert c.apply_normal_day(fills).t_after == 8.5


def test_crash_buy_fill_does_not_move_t():
    """폭락대비만 체결되면 T는 그대로"""
    reg = _reg()
    plan = _plan()
    reg.record_plan(DAY, TICKER, plan.orders)
    reg.record_fill(DAY, TICKER, "buy", 1, 50.00, order_price=53.92)

    fills = reg.build_day_fills(DAY, TICKER, holdings_after=61)
    assert fills.kinds == {FillKind.CRASH_BUY}
    c = TCalculator(40, t=8.0)
    assert c.apply_normal_day(fills).t_after == 8.0


def test_deep_crash_fills_everything_below():
    """종가가 크게 빠지면 그 아래 LOC 이 전부 체결된다.
    본 매수 2건 + 폭락대비 여러 건 → T는 +1 만 오른다."""
    reg = _reg()
    plan = _plan()
    reg.record_plan(DAY, TICKER, plan.orders)

    close = 48.00
    for o in plan.buys:
        if o.price >= close:
            reg.record_fill(DAY, TICKER, "buy", o.qty, close, order_price=o.price)

    fills = reg.build_day_fills(DAY, TICKER, holdings_after=100)
    assert FillKind.STAR_BUY in fills.kinds
    assert FillKind.AVG_BUY in fills.kinds
    assert FillKind.CRASH_BUY in fills.kinds

    c = TCalculator(40, t=8.0)
    assert c.apply_normal_day(fills).t_after == 9.0     # 폭락대비는 T 불변


def test_expire_closes_all_open_orders_including_target_sell():
    """목표매도도 함께 만료된다.

    일반예약(당일 1회)으로 매일 새로 접수하기 때문이다. 평단이 바뀌면
    목표가도 바뀌므로 매일 재계산해서 건다."""
    reg = _reg()
    plan = _plan()
    reg.record_plan(DAY, TICKER, plan.orders)
    reg.expire_open_orders(DAY, TICKER)

    rows = {r.tag: r.status for r in reg.day_orders(DAY, TICKER)}
    assert rows[FillKind.STAR_BUY] == OrderStatus.EXPIRED
    assert rows[FillKind.TARGET_SELL] == OrderStatus.EXPIRED
    assert reg.open_orders(DAY, TICKER) == []


def test_target_sell_does_not_accumulate_across_days():
    """만료를 빼먹으면 보유수량보다 많은 매도 주문이 원장에 쌓인다"""
    reg = _reg()
    for day in ["20260914", "20260915", "20260916"]:
        reg.record_submission(day, TICKER, _plan().orders[0])
        reg.record_submission(day, TICKER, next(
            o for o in _plan().orders if o.tag == FillKind.TARGET_SELL))
        reg.expire_open_orders(day, TICKER)

    still_open = []
    for day in ["20260914", "20260915", "20260916"]:
        still_open += reg.open_orders(day, TICKER)
    assert still_open == []


def test_expired_orders_excluded_from_day_fills():
    reg = _reg()
    plan = _plan()
    reg.record_plan(DAY, TICKER, plan.orders)
    reg.record_fill(DAY, TICKER, "buy", 3, 68.10, order_price=76.02)
    reg.expire_open_orders(DAY, TICKER)

    fills = reg.build_day_fills(DAY, TICKER, holdings_after=63)
    assert fills.kinds == {FillKind.STAR_BUY}


# ================================================================
# 영속성
# ================================================================

def test_survives_restart():
    """프로세스가 죽었다 살아나도 태그가 남아 있어야 한다"""
    tmp = Path(tempfile.mkdtemp()) / "orders.db"
    reg = OrderRegistry(tmp)
    reg.record_plan(DAY, TICKER, _plan().orders)
    del reg

    reg2 = OrderRegistry(tmp)
    rec = reg2.resolve(DAY, TICKER, "buy", price=76.02)
    assert rec.tag == FillKind.STAR_BUY


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
                print(f"  FAIL  {name}: {e}")
    print("-" * 60)
    print("전부 통과" if failed == 0 else f"{failed}건 실패")
    sys.exit(1 if failed else 0)
