"""
================================================================
T값 계산 엔진 (공통)

[2] 리버스모드, [3] 일반모드의 T값 변화 공식 구현
================================================================
"""

from dataclasses import dataclass


@dataclass
class TResult:
    """T값 계산 결과"""
    t_before: float
    t_after: float
    action: str
    detail: str


class TCalculator:
    """
    T값: 회차를 의미하는 핵심 변수
    
    [2] 소진기준: T > division-1
    [3] 보유수량 = 0: 사이클 종료 (T=0)
    """
    
    def __init__(self, division: int):
        self.division = division  # 20 or 40
        self.T = 0.0
    
    # ============================================================
    # 일반모드
    # ============================================================
    
    def add_buy_normal(self, buy_ratio: float = 1.0) -> TResult:
        """
        일반모드 매수 [3]
        - 1회매수 (전체): T + 1
        - 절반매수 (부분): T + 0.5
        """
        before = self.T
        self.T += buy_ratio
        return TResult(
            t_before=before,
            t_after=self.T,
            action="normal_buy",
            detail=f"T={before:.4f} + {buy_ratio} = {self.T:.4f}"
        )
    
    def apply_quarter_sell_normal(self) -> TResult:
        """
        일반모드 쿼터매도 [3]
        - 보유수량 1/4 매도: T * 0.75
        """
        before = self.T
        self.T *= 0.75
        return TResult(
            t_before=before,
            t_after=self.T,
            action="quarter_sell",
            detail=f"T={before:.4f} * 0.75 = {self.T:.4f}"
        )
    
    # ============================================================
    # 리버스모드
    # ============================================================
    
    def first_sell_reverse(self) -> TResult:
        """
        리버스모드 처음매도 [2]
        - 20분할: T *= 0.9 (10등분, T-9)
        - 40분할: T *= 0.95 (20등분, T-19)
        """
        before = self.T
        
        if self.division == 20:
            self.T *= 0.9  # 10등분
            target_t = 9
        else:
            self.T *= 0.95  # 20등분
            target_t = 19
        
        return TResult(
            t_before=before,
            t_after=self.T,
            action="first_sell_moc",
            detail=f"T={before:.4f} → {self.T:.4f} (목표: {target_t})"
        )
    
    def sell_reverse(self, remaining_ratio: float) -> TResult:
        """
        리버스모드 이후 매도 [2]
        - 10등분(20) 또는 20등분(40): T *= remaining_ratio
        """
        before = self.T
        self.T *= remaining_ratio
        return TResult(
            t_before=before,
            t_after=self.T,
            action="reverse_sell",
            detail=f"T={before:.4f} * {remaining_ratio:.4f} = {self.T:.4f}"
        )
    
    def add_quarter_buy_reverse(self) -> TResult:
        """
        리버스모드 쿼터매수 [2]
        - T += (division - T) * 0.25
        """
        before = self.T
        increment = (self.division - self.T) * 0.25
        self.T += increment
        
        return TResult(
            t_before=before,
            t_after=self.T,
            action="quarter_buy_reverse",
            detail=f"T={before:.4f} + ({self.division}-{before:.4f})*0.25 = {self.T:.4f}"
        )
    
    # ============================================================
    # 상태 체크
    # ============================================================
    
    def is_exhausted(self) -> bool:
        """소진 여부: T > division-1 → 리버스모드 발동"""
        return self.T > (self.division - 1)
    
    def is_normal_end(self) -> bool:
        """일반모드 종료: 보유수량=0 → T=0"""
        return self.T == 0
    
    def get_state(self) -> float:
        return self.T
    
    def set_state(self, t: float):
        self.T = t
