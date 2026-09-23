"""
================================================================
일반모드 주문 생성 회귀 테스트

방법론 문서 5번(매수)·6번(매도)의 예시를 그대로 고정한다.

실행:  python tests/test_normal_mode.py
================================================================
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.t_calculator import FillKind
from modes.base_mode import MarketSnapshot, PositionState
from modes.normal_mode import NormalMode


def _plan(**kw):
    market = MarketSnapshot(
        prev_close=kw.pop("prev_close", 0.0),
        current_price=kw.pop("current_price", 0.0),
    )
    return NormalMode(PositionState(**kw)).plan(market)


def _by_tag(plan, tag):
    return [o for o in plan.orders if o.tag == tag]


# ================================================================
# 처음매수 (문서 5-(1))
# ================================================================

def test_entry_buy_doc_example():
    """전일종가 45.93, 1회매수액 617.89
    → 52.82 에 11주, 폭락대비 51.49 / 47.53 각 1주"""
    p = _plan(ticker="TQQQ", division=40, principal=24715.6, fee_rate=0.0007,
              T=0, avg_price=0, holdings=0, cash=617.89 * 40,
              prev_close=45.93, current_price=45.93)

    entry = _by_tag(p, FillKind.ENTRY_BUY)
    assert len(entry) == 1
    assert entry[0].price == 52.82
    assert entry[0].qty == 11

    crash = _by_tag(p, FillKind.CRASH_BUY)
    assert len(crash) == 5
    assert [c.price for c in crash[:2]] == [51.49, 47.53]
    assert all(c.qty == 1 for c in crash)

    # 보유 0 이므로 매도 주문은 없다
    assert p.sells == []


def test_entry_buy_without_prev_close_warns():
    p = _plan(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
              T=0, avg_price=0, holdings=0, cash=20000)
    assert p.orders == []
    assert any("전일종가" in w for w in p.warnings)


# ================================================================
# 전반전 (문서 5-(2))
# ================================================================

def test_first_half_split_doc_example():
    """1회매수액 539.23, 평단 69.75, 별지점 78.12
    → 별지점 78.11 에 3주 / 평단 69.75 에 4주
       폭락대비 67.40 / 59.91 / 53.92 / 49.02

    539.23 ÷ 69.75 = 7.73 → 정수부 7(홀수) → 평단 쪽이 1개 더 많다.
    """
    # TQQQ 40분할 별% = 15 - 0.75T. 69.75×(1+12%) = 78.12 → T=4
    p = _plan(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
              T=4.0, avg_price=69.75, holdings=30, cash=539.23 * 36,
              prev_close=70.0, current_price=70.0)

    assert p.star_point == 78.12
    assert abs(p.unit_amount - 539.23) < 0.01

    star = _by_tag(p, FillKind.STAR_BUY)[0]
    avg = _by_tag(p, FillKind.AVG_BUY)[0]
    assert (star.price, star.qty) == (78.11, 3)   # 별지점은 0.01 차감
    assert (avg.price, avg.qty) == (69.75, 4)     # 평단은 그대로
    assert avg.qty == star.qty + 1                # 홀수 → 평단이 1개 더

    crash = [c.price for c in _by_tag(p, FillKind.CRASH_BUY)]
    assert crash[:4] == [67.40, 59.91, 53.92, 49.02]


def test_first_half_even_split():
    """정수부가 짝수면 수량을 똑같이 나눈다"""
    # 평단 50, 1회매수액 400 → 400/50 = 8 (짝수) → 4주 / 4주
    p = _plan(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
              T=4.0, avg_price=50.0, holdings=30, cash=400.0 * 36,
              prev_close=50.0, current_price=50.0)
    assert _by_tag(p, FillKind.STAR_BUY)[0].qty == 4
    assert _by_tag(p, FillKind.AVG_BUY)[0].qty == 4


def test_first_half_has_no_half_buy_tag():
    """전반전에는 후반전용 단일 주문(HALF_BUY)이 나오면 안 된다"""
    p = _plan(ticker="SOXL", division=20, principal=20000, fee_rate=0.0007,
              T=3.0, avg_price=30.0, holdings=40, cash=5000,
              prev_close=30.0, current_price=30.0)
    assert _by_tag(p, FillKind.HALF_BUY) == []
    assert _by_tag(p, FillKind.STAR_BUY) and _by_tag(p, FillKind.AVG_BUY)


# ================================================================
# 후반전 (문서 5-(3))
# ================================================================

def test_second_half_single_order_doc_example():
    """별지점 59.55 → 59.54 에 9주, 폭락대비 56.85 / 51.68 / 47.37"""
    # TQQQ 40분할 T=24 → 별% = 15-18 = -3% → 평단 = 59.55 / 0.97
    p = _plan(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
              T=24.0, avg_price=59.55 / 0.97, holdings=141, cash=568.50 * 16,
              prev_close=60.0, current_price=60.0)

    assert p.star_point == 59.55
    half = _by_tag(p, FillKind.HALF_BUY)
    assert len(half) == 1
    assert (half[0].price, half[0].qty) == (59.54, 9)

    # 후반전에는 평단 LOC 매수가 없다
    assert _by_tag(p, FillKind.AVG_BUY) == []
    assert _by_tag(p, FillKind.STAR_BUY) == []

    crash = [c.price for c in _by_tag(p, FillKind.CRASH_BUY)]
    assert crash[:3] == [56.85, 51.68, 47.37]


def test_second_half_star_below_avg():
    """후반전에는 별지점이 평단보다 아래로 내려온다"""
    p = _plan(ticker="SOXL", division=40, principal=20000, fee_rate=0.0007,
              T=30.0, avg_price=40.0, holdings=100, cash=3000,
              prev_close=35.0, current_price=35.0)
    assert p.star_point < 40.0


# ================================================================
# 매도 (문서 6번)
# ================================================================

def test_sell_split_doc_example():
    """별지점 59.55, 보유 141주
    → LOC 매도 35주 (1/4, 반올림), 지정가매도 106주 (3/4)"""
    p = _plan(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
              T=24.0, avg_price=59.55 / 0.97, holdings=141, cash=568.50 * 16,
              prev_close=60.0, current_price=60.0)

    quarter = _by_tag(p, FillKind.QUARTER_SELL)[0]
    target = _by_tag(p, FillKind.TARGET_SELL)[0]
    assert quarter.qty == 35
    assert quarter.price == 59.55          # 매도는 별지점 그대로
    assert target.qty == 106
    assert quarter.qty + target.qty == 141


def test_target_sell_pct_by_ticker():
    """TQQQ +15%, SOXL +20%"""
    p = _plan(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
              T=5.0, avg_price=100.0, holdings=40, cash=2000,
              prev_close=100.0, current_price=100.0)
    assert _by_tag(p, FillKind.TARGET_SELL)[0].price == 115.0

    p = _plan(ticker="SOXL", division=40, principal=20000, fee_rate=0.0007,
              T=5.0, avg_price=100.0, holdings=40, cash=2000,
              prev_close=100.0, current_price=100.0)
    assert _by_tag(p, FillKind.TARGET_SELL)[0].price == 120.0


def test_target_sell_submits_at_pre_market():
    """지정가매도는 프리장 시작에 걸어 애프터까지 살려둔다"""
    from modes.base_mode import SubmitWindow
    p = _plan(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
              T=5.0, avg_price=100.0, holdings=40, cash=2000,
              prev_close=100.0, current_price=100.0)
    assert _by_tag(p, FillKind.TARGET_SELL)[0].window == SubmitWindow.PRE_MARKET
    assert _by_tag(p, FillKind.QUARTER_SELL)[0].window == SubmitWindow.ANY


def test_quarter_sell_rounds_half_up():
    """보유 10주 → 10/4 = 2.5 → 3주 (banker's rounding 이면 2주가 된다)"""
    p = _plan(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
              T=5.0, avg_price=100.0, holdings=10, cash=2000,
              prev_close=100.0, current_price=100.0)
    assert _by_tag(p, FillKind.QUARTER_SELL)[0].qty == 3
    assert _by_tag(p, FillKind.TARGET_SELL)[0].qty == 7


# ================================================================
# 가격제한폭 (문서 8번)
# ================================================================

def test_guard_buy_is_single_order_not_two():
    """가격제한폭 대체주문은 단건이어야 한다.

    2분할한 채로 둘 다 현재가×1.15 로 바꾸면
    1회매수액의 두 배가 체결되고 태그 복원도 불가능해진다.
    """
    # 폭락 상황: 평단 100 인데 현재가 60 → 매수가가 현재가보다 한참 위라
    # 증권사가 거부한다. 현재가 +15% 로 단건 대체.
    st = PositionState(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
                       T=8.0, avg_price=100.0, holdings=3, cash=100000)
    p = NormalMode(st).plan(MarketSnapshot(prev_close=62.0, current_price=60.0))

    guard = _by_tag(p, FillKind.GUARD_BUY)
    assert len(guard) == 1
    assert guard[0].price == 69.00                 # 60.00 × 1.15
    assert _by_tag(p, FillKind.STAR_BUY) == []
    assert _by_tag(p, FillKind.AVG_BUY) == []

    # 본 매수 소요액이 1회매수액을 넘지 않아야 한다
    assert guard[0].qty * guard[0].price <= p.unit_amount
    assert not any("단가 충돌" in w for w in p.warnings)
    assert any("가격제한폭" in w for w in p.warnings)


def test_star_and_avg_merge_when_prices_collide():
    """T 가 분할수의 절반에 가까워지면 별% 가 0 에 수렴한다.

    그러면 별지점 매수가(별지점-0.01)와 평단 매수가가 센트 단위에서
    같아진다. 같은 가격에 주문 두 건을 내면 둘 다 체결되는데 태그
    복원이 모호해져서, 1회 매수(+1)를 절반 매수(+0.5)로 잘못 계산한다.
    """
    st = PositionState(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
                       T=19.951, avg_price=40.62, holdings=150, cash=8000)
    p = NormalMode(st).plan(MarketSnapshot(prev_close=41.0, current_price=41.0))

    merged = _by_tag(p, FillKind.MERGED_BUY)
    assert len(merged) == 1
    assert _by_tag(p, FillKind.STAR_BUY) == []
    assert _by_tag(p, FillKind.AVG_BUY) == []
    assert merged[0].qty == int(8000 / (40 - 19.951) / 40.62)
    assert not any("단가 충돌" in w for w in p.warnings)


def test_merged_buy_counts_as_full_buy():
    """병합 매수는 1회매수액 전체이므로 체결 시 T +1"""
    from core.t_calculator import DayFills, TCalculator
    c = TCalculator(40, t=19.951)
    r = c.apply_normal_day(DayFills.from_list([FillKind.MERGED_BUY], holdings_after=160))
    assert abs(r.t_after - 20.951) < 1e-9


def test_no_duplicate_prices_across_t_sweep():
    """T 를 훑어도 매수 단가가 항상 유일해야 한다"""
    for i in range(0, 200):
        T = i * 0.1
        if T >= 20:
            continue
        st = PositionState(ticker="TQQQ", division=40, principal=20000,
                           fee_rate=0.0007, T=T, avg_price=40.62,
                           holdings=150, cash=8000)
        p = NormalMode(st).plan(MarketSnapshot(prev_close=41.0, current_price=41.0))
        prices = [o.price for o in p.buys]
        assert len(prices) == len(set(prices)), f"T={T} 에서 단가 중복: {prices}"


def test_crash_prices_never_collide():
    """주문수량이 크면 1회매수액을 나눈 값이 센트 단위에서 같아진다.
    같은 단가에 주문이 여럿이면 체결 태그를 복원할 수 없다."""
    st = PositionState(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
                       T=5.0, avg_price=0.57, holdings=1000, cash=17000)
    p = NormalMode(st).plan(MarketSnapshot(prev_close=0.57, current_price=0.57))
    prices = [o.price for o in p.buys]
    assert len(prices) == len(set(prices)), f"단가 중복: {prices}"


def test_all_buy_prices_distinct_across_price_levels():
    """여러 가격대에서 매수 단가가 항상 유일해야 한다"""
    for price in [0.5, 2.0, 15.0, 70.0, 500.0]:
        st = PositionState(ticker="TQQQ", division=40, principal=20000,
                           fee_rate=0.0007, T=5.0, avg_price=price,
                           holdings=50, cash=17000)
        p = NormalMode(st).plan(MarketSnapshot(prev_close=price, current_price=price))
        prices = [o.price for o in p.buys]
        assert len(prices) == len(set(prices)), f"평단 {price} 에서 단가 중복"


def test_guard_does_not_fire_early_in_cycle():
    """사이클 초반에는 별지점이 현재가보다 19~20% 위에 놓인다. 정상이다.

    실제 사례(2026-09-23): 평단 151.95, 현재가 150.86, T=1 에서 별지점이
    180.82(+19.85%) 였는데 기준이 18% 라 대체주문이 발동해, 별지점 1주 +
    평단 2주가 2주 @173.49 단건으로 바뀌었다. 방법론이라면 사지 않았을
    가격에도 매수가 들어간다.
    """
    st = PositionState(ticker="SOXL", division=40, principal=20000, fee_rate=0.0007,
                       T=1.0, avg_price=151.95, holdings=3, cash=19543.83)
    p = NormalMode(st).plan(MarketSnapshot(prev_close=150.86, current_price=150.86))

    assert _by_tag(p, FillKind.GUARD_BUY) == []
    star = _by_tag(p, FillKind.STAR_BUY)
    avg = _by_tag(p, FillKind.AVG_BUY)
    assert len(star) == 1 and len(avg) == 1
    assert star[0].price == 180.81 and star[0].qty == 1
    assert avg[0].price == 151.95 and avg[0].qty == 2


def test_guard_still_fires_in_real_crash():
    """평단이 현재가보다 한참 위인 폭락에서는 여전히 발동한다"""
    st = PositionState(ticker="SOXL", division=40, principal=20000, fee_rate=0.0007,
                       T=20.0, avg_price=200.0, holdings=60, cash=10000)
    p = NormalMode(st).plan(MarketSnapshot(prev_close=100.0, current_price=100.0))
    guard = _by_tag(p, FillKind.GUARD_BUY)
    assert len(guard) == 1
    assert guard[0].price == 115.00
    assert _by_tag(p, FillKind.STAR_BUY) == [] and _by_tag(p, FillKind.AVG_BUY) == []


def test_guard_threshold_above_max_star_pct():
    """별% 최대치(20%)보다 기준이 높아야 정상 구간에서 발동하지 않는다"""
    from core.star_point import PRICE_GUARD_PCT
    assert PRICE_GUARD_PCT > 0.20


def test_guard_does_not_fire_on_rally():
    """주가가 매수가보다 한참 위로 올라간 경우는 거부가 아니라
    단순 미체결이다. 여기서 대체주문을 걸면 상승장에서 고점 매수를 한다."""
    st = PositionState(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
                       T=4.5, avg_price=65.94, holdings=32, cash=17000)
    p = NormalMode(st).plan(MarketSnapshot(prev_close=72.0, current_price=81.0))
    assert _by_tag(p, FillKind.GUARD_BUY) == []
    assert _by_tag(p, FillKind.STAR_BUY) != []
    assert all(o.price < 81.0 for o in p.buys), "현재가보다 높은 매수 주문이 있다"


def test_guard_buy_counts_as_full_buy():
    """대체주문 단건 체결은 1회 매수 → T +1"""
    from core.t_calculator import DayFills, TCalculator
    c = TCalculator(40, t=8.0)
    r = c.apply_normal_day(DayFills.from_list([FillKind.GUARD_BUY], holdings_after=40))
    assert r.t_after == 9.0


def test_price_guard_replaces_far_loc_buy():
    """후반전에 별지점이 현재가보다 한참 위면 현재가 +15% 단건으로 대체한다"""
    p = _plan(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
              T=30.0, avg_price=100.0, holdings=100, cash=3000,
              prev_close=60.0, current_price=60.0)
    guard = _by_tag(p, FillKind.GUARD_BUY)
    assert len(guard) == 1
    assert guard[0].price == 69.0               # 60 × 1.15
    assert _by_tag(p, FillKind.HALF_BUY) == []
    assert "가격제한폭" in guard[0].note


def test_no_price_guard_when_close():
    p = _plan(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
              T=5.0, avg_price=100.0, holdings=40, cash=2000,
              prev_close=98.0, current_price=98.0)
    assert all("가격제한폭" not in o.note for o in p.buys)


# ================================================================
# 경계 조건
# ================================================================

def test_exhausted_state_warns_and_skips_buy():
    """T > 분할수-1 이면 매수를 생성하지 않고 경고한다"""
    p = _plan(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
              T=39.5, avg_price=50.0, holdings=400, cash=300,
              prev_close=40.0, current_price=40.0)
    assert p.buys == []
    assert any("소진" in w for w in p.warnings)
    assert p.sells                               # 매도는 계속 나간다


def test_no_cash_skips_buy():
    p = _plan(ticker="TQQQ", division=40, principal=20000, fee_rate=0.0007,
              T=5.0, avg_price=100.0, holdings=40, cash=0.0,
              prev_close=100.0, current_price=100.0)
    assert p.buys == []
    assert p.sells


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
