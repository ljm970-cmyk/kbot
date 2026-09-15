"""
================================================================
별지점 계산기

[2] 리버스모드, [3] 일반모드의 별% 공식 구현
================================================================
"""

from dataclasses import dataclass


@dataclass
class StarPointResult:
    """계산 결과"""
    star_point: float
    buy_price: float  # 매수: star - 0.01
    sell_price: float  # 매도: star (그대로)


class StarPointCalculator:
    """
    [3] 일반모드 별지점 = 평단가 × (1 + 별%)
        TQQQ 20분할: 15 - 1.5*T %
        TQQQ 40분할: 15 - 0.75*T %
        SOXL 20분할: 20 - 2*T %
        SOXL 40분할: 20 - T %

    [2] 리버스모드 별지점 = 직전 5거래일 종가 평균(MA5) 그 자체

    STAR_PCT_REVERSE(-15/-20)는 리버스 '종료판정' 전용 상수이며,
    별지점 계산에는 쓰지 않는다. 두 값은 출처가 다르다.
    """

    # 일반모드 별% 람다 [3]
    STAR_PCT_NORMAL = {
        ('TQQQ', 20): lambda T: (15 - 1.5 * T),
        ('TQQQ', 40): lambda T: (15 - 0.75 * T),
        ('SOXL', 20): lambda T: (20 - 2 * T),
        ('SOXL', 40): lambda T: (20 - T),
    }

    # 리버스 종료판정 기준% (평단 대비) [2]
    STAR_PCT_REVERSE = {
        'TQQQ': -15,  # %
        'SOXL': -20,  # %
    }
    
    def __init__(self, stock: str, division: int, mode: str = "normal"):
        self.stock = stock
        self.division = division
        self.mode = mode
    
    def calculate(self, avg_price: float, T: float, ma5: float = 0.0) -> StarPointResult:
        """
        별지점 계산

        일반모드는 avg_price/T를, 리버스모드는 ma5만 사용한다.
        매수는 항상 0.01 차감 (일반/리버스 공통)
        """
        if self.mode == 'normal':
            return self._calc_normal(avg_price, T)
        else:
            return self._calc_reverse(ma5)
    
    def _calc_normal(self, avg_price: float, T: float) -> StarPointResult:
        """일반모드 [3]"""
        # 별% 계산
        star_pct = self.STAR_PCT_NORMAL[(self.stock, self.division)](T)
        
        # 별지점 (0.01 차감 전에 먼저 반올림해야 매수가가 별지점과 같아지지 않음)
        star = round(avg_price * (1 + star_pct / 100), 2)

        return StarPointResult(
            star_point=star,
            buy_price=round(star - 0.01, 2),  # 매수: 별지점 아래
            sell_price=star                    # 매도: 별지점 그대로
        )
    
    def _calc_reverse(self, ma5: float) -> StarPointResult:
        """
        리버스모드 [2]

        별지점 = 직전 5거래일 종가 평균(MA5)
        """
        star = round(ma5, 2)

        return StarPointResult(
            star_point=star,
            buy_price=round(star - 0.01, 2),  # 매수: 별지점 아래
            sell_price=star                   # 매도: 별지점 그대로
        )

    def is_reverse_end(self, close_price: float, avg_price: float) -> bool:
        """
        리버스모드 종료 조건 [2]

        종가가 '평단' 대비 기준%(-15/-20)보다 크면 종료.
        별지점(MA5)과는 무관한 별도 기준이다.
        """
        star_pct = self.STAR_PCT_REVERSE[self.stock]  # -15 or -20
        threshold = avg_price * (1 + star_pct / 100)
        
        # 종가가 threshold보다 높으면 회복
        return close_price > threshold
