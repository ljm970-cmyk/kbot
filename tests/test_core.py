"""
================================================================
방법론 문서의 수치 예제를 그대로 회귀 테스트로 고정한다.

실행:  python -m pytest tests/test_core.py -v
       또는  python tests/test_core.py
================================================================
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.star_point import (
    crash_buy_prices,
    entry_price,
    guarded_buy_price,
    is_second_half,
    normal_star_point,
    reverse_exit_threshold,
    reverse_sell_qty,
    reverse_star_point,
    round_half_up,
    should_exit_reverse,
    star_pct,
    target_sell_price,
)
from core.t_calculator import DayFills, FillKind, TCalculator, unit_buy_amount


# ================================================================
# 별지점 — 일반모드
# ================================================================

def test_star_point_soxl_20_doc_example():
    """문서 3-(3): SOXL 20분할, 평단 38.30, T=8.6 → 별% 2.8%, 별지점 39.37"""
    assert abs(star_pct("SOXL", 20, 8.6) - 2.8) < 1e-9
    sp = normal_star_point("SOXL", 20, 38.30, 8.6)
    assert sp.star == 39.37
    assert sp.sell_price == 39.37
    assert sp.buy_price == 39.36     # 문서 3-(5): 매수는 0.01 차감


def test_star_pct_zero_at_half():
    """T가 분할수의 절반이면 별%는 정확히 0, 그 이후 음수(후반전)"""
    for ticker, div in [("TQQQ", 20), ("TQQQ", 40), ("SOXL", 20), ("SOXL", 40)]:
        half = div / 2
        assert abs(star_pct(ticker, div, half)) < 1e-9
        assert star_pct(ticker, div, half + 1) < 0
        assert is_second_half(div, half) is True
        assert is_second_half(div, half - 0.0001) is False


def test_star_pct_formulas():
    assert star_pct("TQQQ", 20, 4) == 15 - 1.5 * 4
    assert star_pct("TQQQ", 40, 4) == 15 - 0.75 * 4
    assert star_pct("SOXL", 20, 4) == 20 - 2 * 4
    assert star_pct("SOXL", 40, 4) == 20 - 4


def test_round_half_up_beats_bankers_rounding():
    """내장 round()는 round(2.5)==2 이므로 쓰면 안 된다"""
    assert round(2.5) == 2           # 파이썬 기본 동작 확인
    assert round_half_up(2.5, 0) == 3
    assert round_half_up(0.125, 2) == 0.13


# ================================================================
# 처음매수 / 폭락대비 / 목표가
# ================================================================

def test_entry_price_doc_example():
    """문서 5-(1): 전일종가 45.93 → +15% LOC 52.82"""
    assert entry_price(45.93) == 52.82


def test_crash_buy_prices_doc_example():
    """문서 5-(1): 1회매수액 617.89, 첫 LOC 11주
    → 617.89/12=51.49, /13=47.53"""
    prices = crash_buy_prices(617.89, first_qty=11, count=5)
    assert len(prices) == 5
    assert prices[0] == 51.49
    assert prices[1] == 47.53
    assert prices == sorted(prices, reverse=True)   # 아래로 내려가야 한다


def test_target_sell_price():
    assert target_sell_price("TQQQ", 100.0) == 115.0
    assert target_sell_price("SOXL", 100.0) == 120.0


# ================================================================
# 1회 매수금
# ================================================================

def test_unit_buy_amount_doc_example():
    """문서 4-(2): 원금 20000, 40분할, 첫 매수 478 사용 후 T=1
    → 19522 / 39 = 500.5641..."""
    assert abs(unit_buy_amount(20000, 40, 0) - 500.0) < 1e-9
    second = unit_buy_amount(20000 - 478, 40, 1)
    assert abs(second - 500.5641) < 1e-3


# ================================================================
# T값 — 일반모드
# ================================================================

def test_quarter_sell_doc_example():
    """문서 2-(2): T=7에서 쿼터매도 → 7 × 0.75 = 5.25"""
    c = TCalculator(40, t=7.0)
    r = c.apply_normal_day(DayFills.from_list([FillKind.QUARTER_SELL], holdings_after=100))
    assert r.t_after == 5.25


def test_full_buy_and_half_buy():
    """문서 2-(2): 1회분 전부 매수 +1, 절반 매수 +0.5"""
    c = TCalculator(40, t=7.0)
    r = c.apply_normal_day(DayFills.from_list(
        [FillKind.STAR_BUY, FillKind.AVG_BUY], holdings_after=50))
    assert r.t_after == 8.0

    c = TCalculator(40, t=7.0)
    r = c.apply_normal_day(DayFills.from_list([FillKind.STAR_BUY], holdings_after=50))
    assert r.t_after == 7.5


def test_crash_buy_does_not_move_t():
    """문서 5-(1): 폭락대비 매수체결은 T+0"""
    c = TCalculator(40, t=12.0)
    r = c.apply_normal_day(DayFills.from_list([FillKind.CRASH_BUY], holdings_after=80))
    assert r.t_after == 12.0


def test_crash_buy_with_main_buy():
    """폭락대비가 같이 체결돼도 증분은 본 매수 조합만으로 결정된다"""
    c = TCalculator(40, t=12.0)
    r = c.apply_normal_day(DayFills.from_list(
        [FillKind.STAR_BUY, FillKind.AVG_BUY, FillKind.CRASH_BUY], holdings_after=80))
    assert r.t_after == 13.0


def test_target_sell_alone():
    """3/4 지정가매도 단독 체결 → T × 0.25"""
    c = TCalculator(40, t=20.0)
    r = c.apply_normal_day(DayFills.from_list([FillKind.TARGET_SELL], holdings_after=30))
    assert r.t_after == 5.0


def test_target_sell_then_loc_buy():
    """문서 2-(4): 지정가매도 후 LOC 매수
    1회 매수 → T×0.25 + 1 / 절반 매수 → T×0.25 + 0.5"""
    c = TCalculator(40, t=20.0)
    r = c.apply_normal_day(DayFills.from_list(
        [FillKind.TARGET_SELL, FillKind.STAR_BUY, FillKind.AVG_BUY], holdings_after=40))
    assert r.t_after == 20.0 * 0.25 + 1        # 6.0

    c = TCalculator(40, t=20.0)
    r = c.apply_normal_day(DayFills.from_list(
        [FillKind.TARGET_SELL, FillKind.STAR_BUY], holdings_after=40))
    assert r.t_after == 20.0 * 0.25 + 0.5      # 5.5


def test_both_sells_same_day_closes_cycle():
    """문서 7-(3): 3/4 지정가매도 + 쿼터 LOC매도 동일일 체결 → T=0, 사이클 종료"""
    c = TCalculator(40, t=18.0)
    r = c.apply_normal_day(DayFills.from_list(
        [FillKind.TARGET_SELL, FillKind.QUARTER_SELL], holdings_after=0))
    assert r.t_after == 0.0
    assert r.cycle_closed is True


def test_sell_before_buy_ordering():
    """문서 9번: 매도를 먼저 계산하고 매수를 나중에 계산한다.

    실제로 가능한 조합(지정가매도 + LOC매수)으로 순서를 고정한다.
    순서를 뒤집으면 (20+1)×0.25 = 5.25 가 되어 값이 달라진다."""
    c = TCalculator(40, t=20.0)
    r = c.apply_normal_day(DayFills.from_list(
        [FillKind.TARGET_SELL, FillKind.STAR_BUY, FillKind.AVG_BUY], holdings_after=60))
    assert r.t_after == 20.0 * 0.25 + 1        # 6.0  (≠ (20+1)×0.25 = 5.25)
    assert r.anomalies == []


def test_quarter_sell_and_loc_buy_is_flagged_as_anomaly():
    """쿼터 LOC매도와 LOC매수는 같은 종가로 판정되므로 공존 불가.
    관측되면 이상으로 기록해야 한다."""
    c = TCalculator(40, t=8.0)
    r = c.apply_normal_day(DayFills.from_list(
        [FillKind.QUARTER_SELL, FillKind.STAR_BUY], holdings_after=60))
    assert len(r.anomalies) == 1
    assert "불가능한 조합" in r.anomalies[0]


def test_both_sells_with_buy_ignores_buy():
    """매도 2건 동시 체결일에 매수가 관측되면, 매수를 반영하지 않고
    종료로 확정한 뒤 이상으로 기록한다."""
    c = TCalculator(40, t=18.0)
    r = c.apply_normal_day(DayFills.from_list(
        [FillKind.TARGET_SELL, FillKind.QUARTER_SELL, FillKind.STAR_BUY],
        holdings_after=5))
    assert r.t_after == 0.0
    assert r.cycle_closed is True
    assert len(r.anomalies) >= 1


def test_exhaustion_threshold():
    """문서 5-(3): 40분할은 T>39, 20분할은 T>19 에서 소진"""
    assert TCalculator(40, t=39.0).is_exhausted() is False
    assert TCalculator(40, t=39.5).is_exhausted() is True
    assert TCalculator(20, t=19.0).is_exhausted() is False
    assert TCalculator(20, t=19.1).is_exhausted() is True


# ================================================================
# T값 — 리버스모드
# ================================================================

def test_reverse_doc_example_40():
    """문서 5: 40분할 T=39.5
    → 첫날 MOC 매도 후 37.525
    → 둘째날 쿼터매수 후 38.14375"""
    c = TCalculator(40, t=39.5)
    r1 = c.apply_reverse_day(DayFills.from_list([FillKind.REVERSE_MOC_SELL], holdings_after=190))
    assert abs(r1.t_after - 37.525) < 1e-9

    r2 = c.apply_reverse_day(DayFills.from_list([FillKind.REVERSE_QUARTER_BUY], holdings_after=195))
    assert abs(r2.t_after - 38.14375) < 1e-9


def test_reverse_sell_factor_is_fixed_not_actual_ratio():
    """매도 계수는 실제 수량 비율이 아니라 0.90/0.95 고정이다"""
    c = TCalculator(20, t=19.5)
    r = c.apply_reverse_day(DayFills.from_list([FillKind.REVERSE_SELL], holdings_after=180))
    assert abs(r.t_after - 19.5 * 0.9) < 1e-9


def test_reverse_sell_then_buy_same_day():
    """같은 날 매도·매수가 함께 나면 매도 먼저"""
    c = TCalculator(40, t=38.0)
    r = c.apply_reverse_day(DayFills.from_list(
        [FillKind.REVERSE_SELL, FillKind.REVERSE_QUARTER_BUY], holdings_after=150))
    expected = 38.0 * 0.95
    expected = expected + (40 - expected) * 0.25
    assert abs(r.t_after - expected) < 1e-9


# ================================================================
# 리버스 별지점 / 수량 / 종료
# ================================================================

def test_reverse_star_point_is_5day_close_average():
    """리버스 별지점은 평단과 무관한 직전 5거래일 종가 평균이다"""
    sp = reverse_star_point([48.10, 49.00, 47.50, 50.20, 48.20])
    assert sp.star == 48.60
    assert sp.sell_price == 48.60
    assert sp.buy_price == 48.59
    assert sp.star_pct is None


def test_reverse_star_point_requires_five_closes():
    try:
        reverse_star_point([10.0, 11.0, 12.0])
    except ValueError:
        pass
    else:
        raise AssertionError("5거래일 미만이면 예외가 나야 한다")


def test_reverse_sell_qty_floor_doc_example():
    """문서 2-(1): 198개 → 10등분 19개, 20등분 9개 (내림)"""
    assert reverse_sell_qty(198, 20) == 19
    assert reverse_sell_qty(198, 40) == 9
    assert reverse_sell_qty(200, 40) == 10


def test_reverse_sell_qty_sequence_doc_example():
    """문서 2-(2): 40분할 200개 → 10, 9, 9, 8 ..."""
    holdings = 200
    seq = []
    for _ in range(4):
        q = reverse_sell_qty(holdings, 40)
        seq.append(q)
        holdings -= q
    assert seq == [10, 9, 9, 8]


def test_reverse_exit_doc_example():
    """문서 6-(2): SOXL 평단 40 → 종가가 32를 넘으면 일반모드 복귀"""
    assert reverse_exit_threshold("SOXL", 40.0) == 32.0
    assert should_exit_reverse("SOXL", 40.0, 32.01) is True
    assert should_exit_reverse("SOXL", 40.0, 32.00) is False
    assert should_exit_reverse("TQQQ", 100.0, 85.01) is True


# ================================================================
# 가격제한폭 대응 (문서 8번)
# ================================================================

def test_price_guard():
    """현재가에서 크게 벌어진 LOC 매수는 현재가 +15%로 대체"""
    # 평단 100, 현재가 70 → 30% 괴리 → 대체
    assert guarded_buy_price(100.0, 70.0) == round_half_up(70.0 * 1.15, 2)
    # 5% 괴리 → 원래 가격 유지
    assert guarded_buy_price(100.0, 95.0) == 100.0


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
