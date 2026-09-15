"""
================================================================
별지점 계산기

일반모드와 리버스모드는 별지점의 '정의 자체'가 다르다.

  일반모드   별지점 = 평단가 × (1 + 별%)
             별%  TQQQ 20분할: 15 - 1.5T      TQQQ 40분할: 15 - 0.75T
                  SOXL 20분할: 20 - 2T        SOXL 40분할: 20 - T

  리버스모드 별지점 = 직전 5거래일 종가의 평균
             (평단 대비 -15%/-20%는 별지점이 아니라 '종료조건'이다)

공통
  매수점 = 별지점 - 0.01   (매수·매도가 같은 값에 겹치는 것 방지)
  매도점 = 별지점 그대로
================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Sequence

TICKERS = ("TQQQ", "SOXL")


def round_half_up(value: float, digits: int = 2) -> float:
    """일반 반올림.

    파이썬 내장 round()는 banker's rounding이라 round(2.5)==2 가 된다.
    주문 가격·수량은 방법론대로 '올림 쪽 반올림'을 써야 한다.
    """
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


@dataclass
class StarPoint:
    """별지점 계산 결과"""
    star: float          # 별지점
    buy_price: float     # 별지점 - 0.01
    sell_price: float    # 별지점 그대로
    star_pct: float | None = None   # 일반모드 별% (리버스는 None)

    def __str__(self) -> str:
        pct = f" (별%={self.star_pct:+.4f}%)" if self.star_pct is not None else ""
        return f"별지점 {self.star:.2f} / 매수 {self.buy_price:.2f} / 매도 {self.sell_price:.2f}{pct}"


# ================================================================
# 일반모드
# ================================================================

#: 별% 공식. T가 분할수의 절반일 때 정확히 0이 되고, 그 이후 음수가 된다.
STAR_PCT_FORMULA = {
    ("TQQQ", 20): lambda t: 15.0 - 1.50 * t,
    ("TQQQ", 40): lambda t: 15.0 - 0.75 * t,
    ("SOXL", 20): lambda t: 20.0 - 2.00 * t,
    ("SOXL", 40): lambda t: 20.0 - 1.00 * t,
}

#: 목표 수익률 (3/4 지정가매도) 및 리버스 종료 기준
TARGET_PCT = {"TQQQ": 0.15, "SOXL": 0.20}


def star_pct(ticker: str, division: int, t: float) -> float:
    """별% 값 (백분율 숫자, 예: 2.8 == 2.8%)"""
    key = (ticker.upper(), division)
    try:
        return STAR_PCT_FORMULA[key](t)
    except KeyError:
        raise ValueError(f"지원하지 않는 조합: {ticker} {division}분할")


def normal_star_point(ticker: str, division: int, avg_price: float, t: float) -> StarPoint:
    """일반모드 별지점.

    >>> sp = normal_star_point("SOXL", 20, 38.30, 8.6)
    >>> sp.star
    39.37
    >>> sp.buy_price
    39.36
    """
    if avg_price <= 0:
        raise ValueError("평단가가 0이면 별지점을 계산할 수 없습니다 (처음매수 경로 사용)")
    pct = star_pct(ticker, division, t)
    star = round_half_up(avg_price * (1 + pct / 100), 2)
    return StarPoint(
        star=star,
        buy_price=round_half_up(star - 0.01, 2),
        sell_price=star,
        star_pct=pct,
    )


def is_second_half(division: int, t: float) -> bool:
    """후반전 여부. T ≥ 분할수/2 부터 별%가 0 이하가 된다."""
    return t >= (division / 2)


def entry_price(prev_close: float) -> float:
    """처음매수(T=0) LOC 가격 = 전일종가 × 1.15"""
    return round_half_up(prev_close * 1.15, 2)


def target_sell_price(ticker: str, avg_price: float) -> float:
    """3/4 지정가매도 목표가. TQQQ 평단+15%, SOXL 평단+20%."""
    return round_half_up(avg_price * (1 + TARGET_PCT[ticker.upper()]), 2)


def round_down(value: float, digits: int = 2) -> float:
    """내림. 폭락대비 매수가처럼 문서가 절사를 쓰는 자리에 사용."""
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN))


def crash_buy_prices(unit_amount: float, first_qty: int, count: int = 5) -> list[float]:
    """폭락대비 LOC 매수 가격 목록 (각 1주씩, 최대 5개).

    1회 매수액을 (본주문 수량+1), (+2) ... 로 나눈 값이 그대로 가격이 된다.
    반올림이 아니라 **내림**이다. 문서 예제 617.89/14 = 44.135 → 44.13,
    568.5/12 = 47.375 → 47.37 이 반올림과 갈리며 둘 다 내림 값이다.

    >>> crash_buy_prices(617.89, 11, 3)
    [51.49, 47.53, 44.13]
    >>> crash_buy_prices(539.23, 7, 4)
    [67.4, 59.91, 53.92, 49.02]
    """
    prices = []
    for n in range(first_qty + 1, first_qty + 1 + count):
        if n <= 0:
            continue
        prices.append(round_down(unit_amount / n, 2))
    return prices


# ================================================================
# 리버스모드
# ================================================================

def reverse_star_point(recent_closes: Sequence[float]) -> StarPoint:
    """리버스모드 별지점 = 직전 5거래일 종가의 평균.

    평단과 무관하다. 이 값 위에서 매도(10등분/20등분),
    아래에서 쿼터매수(잔금/4)를 건다.

    >>> reverse_star_point([48.10, 49.00, 47.50, 50.20, 48.20]).star
    48.6
    """
    closes = [c for c in recent_closes if c and c > 0]
    if len(closes) < 5:
        raise ValueError(f"직전 5거래일 종가가 필요합니다 (받은 유효값 {len(closes)}건)")
    star = round_half_up(sum(closes[:5]) / 5, 2)
    return StarPoint(
        star=star,
        buy_price=round_half_up(star - 0.01, 2),
        sell_price=star,
        star_pct=None,
    )


def reverse_exit_threshold(ticker: str, avg_price: float) -> float:
    """리버스모드 종료 기준가 = 평단 × (1 - 15%/20%)"""
    return round_half_up(avg_price * (1 - TARGET_PCT[ticker.upper()]), 2)


def should_exit_reverse(ticker: str, avg_price: float, close_price: float) -> bool:
    """리버스 → 일반 복귀 판정.

    종가가 평단 대비 -15%(TQQQ) / -20%(SOXL)보다 커지는 것을 확인한
    이후부터 일반모드로 돌아간다. T값은 그대로 이어서 쓴다.
    """
    if avg_price <= 0 or close_price <= 0:
        return False
    return close_price > reverse_exit_threshold(ticker, avg_price)


def reverse_sell_qty(holdings: int, division: int) -> int:
    """리버스 매도 수량. 20분할 10등분 / 40분할 20등분, 내림.

    >>> reverse_sell_qty(198, 20), reverse_sell_qty(198, 40)
    (19, 9)
    """
    divisor = 10 if division == 20 else 20
    return max(0, holdings // divisor)


# ================================================================
# 방법론 8번 — 증권사 가격제한폭(±20%) 대응
# ================================================================

#: 증권사가 주문을 거부하기 시작하는 현재가 대비 괴리 (보수적으로 18%)
PRICE_GUARD_PCT = 0.18


def needs_price_guard(order_price: float, current_price: float) -> bool:
    """주문가가 현재가보다 너무 **높아** 거부될 가능성이 있는지.

    방법론 8번은 "큰 하락장에서 매수매도가 걸리지 않을 경우" 를 다룬다.
    폭락으로 평단이 현재가보다 한참 위가 되면 증권사가 주문을 거부하므로,
    현재가 +15% 에 다시 걸어 반드시 종가 매수가 되게 한다.

    반대 방향은 해당하지 않는다. 주가가 우리 매수가보다 한참 위로
    올라간 경우는 주문이 거부되는 게 아니라 그냥 체결되지 않는 것이고,
    그게 정상이다. 여기서 대체주문을 걸면 상승장에서 고점 매수를 하게 되어
    전략이 뒤집힌다.

    (절댓값으로 비교하던 버전은 종가가 크게 오른 날 현재가 +15% 에
     매수를 걸어 실제로 체결시켰다.)
    """
    if current_price <= 0 or order_price <= 0:
        return False
    return (order_price - current_price) / current_price >= PRICE_GUARD_PCT


def guarded_buy_price(order_price: float, current_price: float) -> float:
    """무조건 매수를 의도하는 LOC 매수의 대체가.

    평단/별지점 LOC 매수가 거부될 상황이면 현재가 +15%로 걸어
    반드시 종가 매수가 되게 한다 (방법론 8번).

    order_price 에 float("inf") 를 넘기면 항상 대체가를 돌려준다.
    """
    if needs_price_guard(order_price, current_price):
        return round_half_up(current_price * 1.15, 2)
    return round_half_up(order_price, 2)


# ================================================================
# 구버전 호환
# ================================================================

class StarPointCalculator:
    """구버전 인터페이스 호환 래퍼.

    리버스모드는 5거래일 종가가 필요하므로 `recent_closes`를 넘겨야 한다.
    신규 코드에서는 모듈 함수를 직접 쓰는 편이 낫다.
    """

    def __init__(self, stock: str, division: int, mode: str = "normal"):
        self.stock = stock.upper()
        self.division = division
        self.mode = mode

    def calculate(self, avg_price: float, T: float = 0.0,
                  recent_closes: Sequence[float] | None = None) -> StarPoint:
        if self.mode == "normal":
            return normal_star_point(self.stock, self.division, avg_price, T)
        if recent_closes is None:
            raise ValueError(
                "리버스모드 별지점은 직전 5거래일 종가가 필요합니다. "
                "KiwoomAPIClient.get_recent_closes(ticker, exchange, 5) 결과를 넘기세요. "
                "표시 목적이면 state['last_star_point'] 를 쓰세요."
            )
        return reverse_star_point(recent_closes)

    @staticmethod
    def recover_price(ticker: str, avg_price: float) -> float:
        """리버스 종료(일반모드 복귀) 기준가.

        별지점(직전 5거래일 종가 평균)과는 출처가 다른 별도 기준이다.
        """
        return reverse_exit_threshold(ticker, avg_price)

    def is_reverse_end(self, close_price: float, avg_price: float) -> bool:
        return should_exit_reverse(self.stock, avg_price, close_price)
