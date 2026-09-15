"""
================================================================
리버스모드 회귀 테스트 — 방법론 문서 예제 고정

실행:  python tests/test_reverse_mode.py
================================================================
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.t_calculator import DayFills, FillKind, TCalculator
from kiwoom.constants import TradeType
from modes.base_mode import MarketSnapshot, PositionState
from modes.reverse_mode import ReverseMode, should_enter_reverse


def _state(**kw) -> PositionState:
    base = dict(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
                T=39.5, avg_price=50.0, holdings=200, cash=400.0,
                reverse_first_day=False)
    base.update(kw)
    return PositionState(**base)


def _plan(market: MarketSnapshot = None, **kw):
    return ReverseMode(_state(**kw)).plan(market or MarketSnapshot())


def _by_tag(plan, tag):
    return [o for o in plan.orders if o.tag == tag]


FIVE_CLOSES = [48.10, 49.00, 47.50, 50.20, 48.20]   # 평균 48.60


# ================================================================
# 발동 조건
# ================================================================

def test_entry_threshold():
    """20분할 T>19, 40분할 T>39 에서 리버스 발동"""
    assert should_enter_reverse(39.0, 40) is False
    assert should_enter_reverse(39.5, 40) is True
    assert should_enter_reverse(19.0, 20) is False
    assert should_enter_reverse(19.1, 20) is True


# ================================================================
# 첫날 — MOC 매도
# ================================================================

def test_first_day_moc_only():
    """문서 2-(1): 40분할 200개 → 20등분 10개 MOC. 매수는 없다."""
    p = _plan(reverse_first_day=True, holdings=200, division=40)
    sells = _by_tag(p, FillKind.REVERSE_MOC_SELL)
    assert len(sells) == 1
    assert sells[0].qty == 10
    assert sells[0].trade_type == TradeType.MOC
    assert sells[0].price is None          # MOC 는 단가 없음
    assert p.buys == []                     # 첫날 매수 없음


def test_first_day_20_division():
    """20분할 200개 → 10등분 20개"""
    p = _plan(reverse_first_day=True, holdings=200, division=20, T=19.5)
    assert _by_tag(p, FillKind.REVERSE_MOC_SELL)[0].qty == 20


def test_first_day_floor_doc_example():
    """문서 2-(1): 198개 → 10등분 19개, 20등분 9개 (내림)"""
    assert _by_tag(_plan(reverse_first_day=True, holdings=198, division=20, T=19.5),
                   FillKind.REVERSE_MOC_SELL)[0].qty == 19
    assert _by_tag(_plan(reverse_first_day=True, holdings=198, division=40),
                   FillKind.REVERSE_MOC_SELL)[0].qty == 9


def test_first_day_needs_no_closes():
    """첫날은 별지점 계산 없이 MOC 이므로 시세가 비어도 주문이 나온다"""
    p = _plan(reverse_first_day=True, holdings=200)
    assert len(p.orders) == 1
    assert p.warnings == []


# ================================================================
# 둘째날 이후 — LOC 매도
# ================================================================

def test_sell_sequence_doc_example():
    """문서 2-(2): 40분할 200개 → 10, 9, 9, 8 순으로 매도"""
    holdings = 200
    seq = []
    # 첫날
    p = _plan(reverse_first_day=True, holdings=holdings)
    q = _by_tag(p, FillKind.REVERSE_MOC_SELL)[0].qty
    seq.append(q)
    holdings -= q
    # 둘째날 이후
    for _ in range(3):
        p = _plan(MarketSnapshot(recent_closes=FIVE_CLOSES),
                  holdings=holdings, cash=5000.0)   # 소진 전 (1단계) 상태
        q = _by_tag(p, FillKind.REVERSE_SELL)[0].qty
        seq.append(q)
        holdings -= q
    assert seq == [10, 9, 9, 8]


def test_star_point_is_5day_average():
    """리버스 별지점 = 직전 5거래일 종가 평균. 평단과 무관하다."""
    p = _plan(MarketSnapshot(recent_closes=FIVE_CLOSES), avg_price=999.0, cash=5000.0)
    assert p.star_point == 48.60
    assert _by_tag(p, FillKind.REVERSE_SELL)[0].price == 48.60   # 매도는 별지점 그대로


def test_missing_closes_blocks_orders():
    """5거래일 종가가 없으면 주문을 만들지 않고 경고한다"""
    p = _plan(MarketSnapshot(recent_closes=[48.0, 49.0]))
    assert p.orders == []
    assert any("별지점" in w for w in p.warnings)


# ================================================================
# 쿼터매수
# ================================================================

def test_quarter_buy_is_cash_over_four():
    """문서 3-(2): 잔금 700 → 700/4 = 175 로 매수 시도"""
    p = _plan(MarketSnapshot(recent_closes=FIVE_CLOSES), cash=700.0, holdings=190)
    assert abs(p.unit_amount - 175.0) < 1e-9
    buys = _by_tag(p, FillKind.REVERSE_QUARTER_BUY)
    assert len(buys) == 1
    assert buys[0].price == 48.59            # 별지점 - 0.01
    assert buys[0].qty == int(175.0 / 48.59)  # 3주


def test_quarter_buy_doc_example_shape():
    """문서 3-(3): 쿼터매수액 544.47, 별지점 48.61 → 48.61에 11주,
    폭락대비 45.37(/12), 41.88(/13), ...

    문서의 세 번째 값 38.83 은 544.47/14 = 38.8907 과 맞지 않는다.
    네 번째 값 36.29 는 544.47/15 = 36.2980 과 일치하므로
    38.83 은 38.89 의 오타로 보인다.
    """
    closes = [48.62] * 5      # 별지점 48.62 → 매수가 48.61
    p = _plan(MarketSnapshot(recent_closes=closes), cash=544.47 * 4, holdings=190)
    buys = _by_tag(p, FillKind.REVERSE_QUARTER_BUY)
    assert buys[0].price == 48.61
    assert buys[0].qty == 11

    crash = [c.price for c in _by_tag(p, FillKind.CRASH_BUY)]
    assert crash[0] == 45.37     # 544.47/12
    assert crash[1] == 41.88     # 544.47/13
    assert crash[3] == 36.29     # 544.47/15


# ================================================================
# 리버스 소진 (라오어 2026-08-02 보강)
# ================================================================

def test_reverse_exhaustion_switches_to_moc_only():
    """쿼터매수로 1주도 못 사면 매수 시도 없이 MOC 매도만 시행한다.

    별지점 48.60 → 매수가 48.59. 잔금 190 → 쿼터매수 47.50 < 48.59 → 소진.
    """
    p = _plan(MarketSnapshot(recent_closes=FIVE_CLOSES), cash=190.0, holdings=190)
    assert p.exhausted is True
    assert p.buys == []                                  # 매수 시도 없음
    assert _by_tag(p, FillKind.REVERSE_SELL) == []       # LOC 매도 아님
    moc = _by_tag(p, FillKind.REVERSE_MOC_SELL)
    assert len(moc) == 1
    assert moc[0].trade_type == TradeType.MOC
    assert moc[0].qty == 9                               # 190÷20 내림
    assert any("리버스 소진" in w for w in p.warnings)


def test_zero_cash_is_exhausted():
    """잔금 0은 당연히 소진 → MOC 매도만"""
    p = _plan(MarketSnapshot(recent_closes=FIVE_CLOSES), cash=0.0, holdings=190)
    assert p.exhausted is True
    assert p.buys == []
    assert len(_by_tag(p, FillKind.REVERSE_MOC_SELL)) == 1


def test_not_exhausted_when_quarter_buys_one_share():
    """쿼터매수로 딱 1주 살 수 있으면 아직 소진이 아니다.

    매수가 48.59 → 잔금 194.36 이면 쿼터매수 48.59 로 정확히 1주.
    """
    p = _plan(MarketSnapshot(recent_closes=FIVE_CLOSES), cash=48.59 * 4, holdings=190)
    assert p.exhausted is False
    buys = _by_tag(p, FillKind.REVERSE_QUARTER_BUY)
    assert len(buys) == 1 and buys[0].qty == 1
    assert len(_by_tag(p, FillKind.REVERSE_SELL)) == 1   # LOC 매도 유지


def test_t_over_threshold_does_not_stop_quarter_buy():
    """T가 39를 한참 넘어도 쿼터매수가 가능하면 계속한다 (보강 규칙 핵심)"""
    p = _plan(MarketSnapshot(recent_closes=FIVE_CLOSES),
              T=39.99, cash=2000.0, holdings=190)
    assert p.exhausted is False
    assert len(_by_tag(p, FillKind.REVERSE_QUARTER_BUY)) == 1


def test_exit_still_possible_after_reverse_exhaustion():
    """리버스 소진 상태여도 종가가 회복되면 일반모드로 복귀한다"""
    m = ReverseMode(_state(ticker="SOXL", avg_price=40.0, cash=0.0, holdings=100))
    p = m.plan(MarketSnapshot(recent_closes=FIVE_CLOSES))
    assert p.exhausted is True
    assert m.check_exit(33.0) == "normal"


# ================================================================
# T값
# ================================================================

def test_t_doc_example():
    """문서 5: 40분할 T=39.5 → 첫날 MOC 후 37.525 → 쿼터매수 후 38.14375"""
    c = TCalculator(40, t=39.5)
    r1 = c.apply_reverse_day(DayFills.from_list([FillKind.REVERSE_MOC_SELL], holdings_after=190))
    assert abs(r1.t_after - 37.525) < 1e-9
    r2 = c.apply_reverse_day(DayFills.from_list([FillKind.REVERSE_QUARTER_BUY], holdings_after=193))
    assert abs(r2.t_after - 38.14375) < 1e-9


def test_t_sell_factor_is_fixed():
    """매도 계수는 실제 수량비율이 아니라 0.90/0.95 고정"""
    c = TCalculator(20, t=19.5)
    r = c.apply_reverse_day(DayFills.from_list([FillKind.REVERSE_SELL], holdings_after=180))
    assert abs(r.t_after - 19.5 * 0.90) < 1e-9


def test_crash_buy_does_not_move_t_in_reverse():
    """리버스에서도 폭락대비 체결은 T를 움직이지 않는다"""
    c = TCalculator(40, t=38.0)
    r = c.apply_reverse_day(DayFills.from_list([FillKind.CRASH_BUY], holdings_after=190))
    assert r.t_after == 38.0


# ================================================================
# 종료 조건
# ================================================================

def test_exit_doc_example():
    """문서 6-(2): SOXL 평단 40 → 종가가 32를 넘으면 일반모드 복귀"""
    m = ReverseMode(_state(ticker="SOXL", avg_price=40.0))
    assert m.exit_threshold() == 32.0
    assert m.check_exit(32.01) == "normal"
    assert m.check_exit(32.00) is None
    assert m.check_exit(31.0) is None


def test_exit_tqqq_threshold():
    m = ReverseMode(_state(ticker="TQQQ", avg_price=100.0))
    assert m.exit_threshold() == 85.0
    assert m.check_exit(85.01) == "normal"


def test_t_is_carried_over_on_exit():
    """복귀 시 T값은 리버스에서 쓰던 값을 그대로 이어 쓴다 (문서 6-(4))"""
    st = _state(ticker="SOXL", avg_price=40.0, T=38.14375)
    m = ReverseMode(st)
    assert m.check_exit(33.0) == "normal"
    assert st.T == 38.14375        # 리셋되지 않아야 한다


# ================================================================
# 잔여물량 경계
# ================================================================

def test_zero_qty_warns_instead_of_forcing_one():
    """20등분해서 0주가 되면 억지로 1주를 걸지 않고 경고한다"""
    p = _plan(MarketSnapshot(recent_closes=FIVE_CLOSES), holdings=19, division=40, cash=5000.0)
    assert _by_tag(p, FillKind.REVERSE_SELL) == []
    assert any("0주" in w for w in p.warnings)


def test_zero_avg_price_blocks_orders():
    """평단 0이면 종료 판정이 항상 False 가 되어 리버스에서 못 빠져나온다"""
    p = _plan(MarketSnapshot(recent_closes=FIVE_CLOSES), avg_price=0.0, holdings=100)
    assert p.orders == []
    assert any("평단이 0" in w for w in p.warnings)


def test_no_holdings_warns():
    p = _plan(MarketSnapshot(recent_closes=FIVE_CLOSES), holdings=0)
    assert p.orders == []
    assert any("보유수량이 0" in w for w in p.warnings)


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
