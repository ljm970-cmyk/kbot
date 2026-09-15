"""
================================================================
수수료 정책

거래 수수료율은 계좌마다 다르므로 사용자가 직접 입력한다.
최소수수료도 계좌 조건에 따라 있을 수도, 없을 수도 있어
설정값으로 뺐다 (기존 코드는 $2.50 을 하드코딩했다).

방법론 10: 매도 대금이 발생하면 즉시 전략 잔금에 더해지며,
거래 수수료 차감 후 금액을 사용한다.
================================================================
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeePolicy:
    """계좌별 수수료 구조

    Attributes:
        rate: 거래 수수료율. 0.0007 == 0.07%
        min_fee: 최소 수수료(USD). 0 이면 미적용.
        sec_fee_rate: 매도 시 SEC 수수료율. 매도 금액에만 붙는다.
        taf_per_share: 매도 시 FINRA TAF (주당). 상한 taf_cap.
        taf_cap: TAF 상한(USD)
    """
    rate: float = 0.0007
    min_fee: float = 0.0
    sec_fee_rate: float = 0.0000278
    taf_per_share: float = 0.000166
    taf_cap: float = 8.30

    @property
    def display_percent(self) -> float:
        """표시용: 0.0007 → 0.07"""
        return self.rate * 100


#: SEC/TAF 요율은 해마다 바뀐다. 위 기본값은 대략치이므로
#: 정확한 정산이 필요하면 증권사 고지 기준으로 갱신할 것.
DEFAULT_FEE_POLICY = FeePolicy()


def calculate_fee(
    trade_amount: float,
    qty: int = 0,
    is_sell: bool = False,
    policy: FeePolicy = DEFAULT_FEE_POLICY,
) -> float:
    """거래 1건의 수수료.

    Args:
        trade_amount: 체결금액 (체결단가 × 수량)
        qty: 체결수량 (매도 TAF 계산용)
        is_sell: 매도 여부
    """
    if trade_amount <= 0:
        return 0.0

    fee = trade_amount * policy.rate
    if policy.min_fee > 0:
        fee = max(fee, policy.min_fee)

    if is_sell:
        fee += trade_amount * policy.sec_fee_rate
        if qty > 0:
            fee += min(qty * policy.taf_per_share, policy.taf_cap)

    return round(fee, 4)


def apply_buy(cash: float, trade_amount: float, fee: float) -> float:
    """매수 후 잔금 = 잔금 - 체결금액 - 수수료"""
    return cash - trade_amount - fee


def apply_sell(cash: float, trade_amount: float, fee: float) -> float:
    """매도 후 잔금 = 잔금 + 체결금액 - 수수료 (방법론 10)"""
    return cash + trade_amount - fee


def policy_from_rate(rate: float, min_fee: float = 0.0) -> FeePolicy:
    """사용자 입력(수수료율)으로 정책 생성"""
    return FeePolicy(rate=rate, min_fee=min_fee)
