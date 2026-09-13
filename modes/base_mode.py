"""
================================================================
추상 기본 클래스

normal_mode, reverse_mode의 공통 인터페이스 정의
================================================================
"""

from abc import ABC, abstractmethod
from typing import List, Optional


class BaseTradingMode(ABC):
    """매매 모드 추상 클래스"""
    
    def __init__(self, ticker: str, division: int, principal: float, fee_rate: float):
        self.ticker = ticker
        self.division = division
        self.principal = principal
        self.fee_rate = fee_rate
    
    @abstractmethod
    def generate_orders(self, state: dict) -> List[dict]:
        """
        다음 거래일 주문 생성
        
        Returns:
            [{"type": "LOC_BUY", "api_id": "ust21200", ...}, ...]
        """
        pass
    
    @abstractmethod
    def apply_fill(self, state: dict, fill: dict) -> dict:
        """
        체결 내역 반영 → 상태 업데이트
        
        Returns:
            업데이트된 state dict
        """
        pass
    
    @abstractmethod
    def check_transition(self, state: dict) -> Optional[str]:
        """
        모드 전환 체크
        
        Returns:
            "reverse", "normal", or None (유지)
        """
        pass
    
    def calculate_buy_amount(self, cash: float, T: float) -> float:
        """
        1회 매수금 계산 [2][3]
        
        잔금 / (division - T)
        """
        if T >= self.division:
            return 0
        return cash / (self.division - T)
