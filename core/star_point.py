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
    recover_price: float = 0.0  # 리버스 회복 기준


class StarPointCalculator:
    """
    별지점 = 평단가 × (1 + 별%)
    
    [3] 일반모드 별%:
        TQQQ 20분할: 15 - 1.5*T %
        TQQQ 40분할: 15 - 0.75*T %
        SOXL 20분할: 20 - 2*T %
        SOXL 40분할: 20 - T %
    
    [2] 리버스모드 별%:
        TQQQ: -15% (평단 대비)
        SOXL: -20% (평단 대비)
    """
    
    # 일반모드 별% 람다 [3]
    STAR_PCT_NORMAL = {
        ('TQQQ', 20): lambda T: (15 - 1.5 * T),
        ('TQQQ', 40): lambda T: (15 - 0.75 * T),
        ('SOXL', 20): lambda T: (20 - 2 * T),
        ('SOXL', 40): lambda T: (20 - T),
    }
    
    # 리버스모드 별% [2]
    STAR_PCT_REVERSE = {
        'TQQQ': -15,  # %
        'SOXL': -20,  # %
    }
    
    def __init__(self, stock: str, division: int, mode: str = "normal"):
        self.stock = stock
        self.division = division
        self.mode = mode
    
    def calculate(self, avg_price: float, T: float) -> StarPointResult:
        """
        별지점 계산
        
        매수는 항상 0.01 차감 (일반/리버스 공통)
        """
        if self.mode == 'normal':
            return self._calc_normal(avg_price, T)
        else:
            return self._calc_reverse(avg_price)
    
    def _calc_normal(self, avg_price: float, T: float) -> StarPointResult:
        """일반모드 [3]"""
        # 별% 계산
        star_pct = self.STAR_PCT_NORMAL[(self.stock, self.division)](T)
        
        # 별지점
        star = avg_price * (1 + star_pct / 100)
        
        # 매수는 0.01 차감, 매도는 그대로
        buy = round(star - 0.01, 2)
        sell = round(star, 2)
        
        return StarPointResult(
            star_point=round(star, 2),
            buy_price=buy,
            sell_price=sell
        )
    
    def _calc_reverse(self, avg_price: float) -> StarPointResult:
        """
        리버스모드 [2]
        
        별지점 = 평단 × (1 - 15% or -20%)
        """
        star_pct = self.STAR_PCT_REVERSE[self.stock]
        
        # 별지점 (음수)
        star = avg_price * (1 + star_pct / 100)
        
        # 회복 기준: 별지점보다 높으면 일반모드 복귀
        recover = round(star, 2)
        
        return StarPointResult(
            star_point=round(star, 2),
            buy_price=round(star - 0.01, 2),  # 매수: 별지점 아래
            sell_price=round(star, 2),         # 매도: 별지점 위에서
            recover_price=recover
        )
    
    def is_reverse_end(self, close_price: float, avg_price: float) -> bool:
        """
        리버스모드 종료 조건 [2]
        
        종가가 평단 대비 기준% 이상 회복
        """
        star_pct = self.STAR_PCT_REVERSE[self.stock]  # -15 or -20
        threshold = avg_price * (1 + star_pct / 100)
        
        # 종가가 threshold보다 높으면 회복
        return close_price > threshold
