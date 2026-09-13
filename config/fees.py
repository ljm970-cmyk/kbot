"""
================================================================
수수료 정책

기존 [4]의 manual_count, trapped 등 금액 계산 패턴에
수수료 차감 로직을 통합
================================================================
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FeeStructure:
    """계좌별 수수료 구조 (사용자 직접 입력 0~1%)"""
    rate: float  # 0.0007 = 0.07%
    min_fee: float = 2.50  # USD (키움 해외주식)
    
    @property
    def display_percent(self) -> float:
        """표시용: 0.0007 → 0.07"""
        return self.rate * 100


def calculate_trade_fee(trade_amount: float, fee_rate: float, is_sell: bool = False) -> float:
    """
    거래 수수료 계산
    
    기존 [4]의 principal * local_ratio 등
    금액 비율 계산과 동일한 패턴
    """
    # 기본 수수료
    calculated = trade_amount * fee_rate
    actual = max(calculated, 2.50)  # 최소 $2.50
    
    # 매도 시 추가 수수료 (키움 해외주식)
    if is_sell:
        # FINRA TAF: $0.000145/주, 최대 $7.27
        # SEC Fee: 거래금액 * 0.000008 (2024 기준, 변동 가능)
        sec_fee = trade_amount * 0.000008
        actual += sec_fee
    
    return round(actual, 2)


def apply_fee_to_cash(cash: float, trade_amount: float, fee: float, is_buy: bool) -> float:
    """
    잔금에서 수수료 차감
    
    기존 [4]의 trapped_pnl 수정 로직과 유사:
    실제 잔금 = 거래금액 ± 수수료 반영
    """
    if is_buy:
        # 매수: 거래금액 + 수수료를 잔금에서 감소
        return round(cash - trade_amount - fee, 2)
    else:
        # 매도: 거래금액 - 수수료를 잔금에 증가
        return round(cash + trade_amount - fee, 2)
