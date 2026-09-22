"""
================================================================
자금 점검 — 매일 부족분을 채우는 운용 방식

원금은 장부상 금액(예: $20,000)으로 두고, 증권사 계좌에는 그날 주문에
필요한 달러만 매일 채워 넣는 방식을 지원한다. 무한매수법 계산
(1회매수액 = 잔금 ÷ 남은 분할)은 장부 기준으로 돌고, 실제 계좌에는
그날 주문을 받을 만큼만 있으면 된다.

이 방식에서 예수금 부족은 장부 이상이 아니라 "아직 입금 전" 이라는
일시적 상태다. 그래서
  - 회로차단기를 걸지 않는다 (걸면 입금해도 풀리지 않는다)
  - 매도는 그대로 낸다 (돈이 모자란 건 매수 문제다)
  - 매수만 그날 건너뛰고, 필요한 입금액을 알린다
================================================================
"""

from __future__ import annotations

from dataclasses import dataclass

#: 체결가가 주문가보다 조금 높게 잡히거나 수수료가 붙는 것을 감안한 여유분
SAFETY_MARGIN = 0.005      # 0.5%


@dataclass
class FundingCheck:
    need: float          # 그날 매수 주문을 모두 받으려면 필요한 금액
    available: float     # 실제 매수 가능 금액 (미수불가)

    @property
    def shortfall(self) -> float:
        return max(0.0, self.need - self.available)

    @property
    def ok(self) -> bool:
        return self.shortfall <= 0.0

    def topup_text(self) -> str:
        """입금 안내 한 줄"""
        if self.need <= 0:
            return "  매수 주문 없음 — 입금 불필요"
        if self.ok:
            return (f"  필요 ${self.need:,.2f} / 가능 ${self.available:,.2f} — 충분")
        return (f"  필요 ${self.need:,.2f} / 가능 ${self.available:,.2f}\n"
                f"  → ${self.shortfall:,.2f} 입금이 필요합니다")


def buy_need(plan, fee_rate: float) -> float:
    """매수 주문을 모두 받으려면 필요한 금액.

    증권사는 예약주문을 실주문으로 넘길 때 주문가 × 수량으로 매수대금을
    묶는다. LOC 는 실제로는 종가(주문가 이하)에 체결되지만, 접수 시점에는
    주문가 기준으로 확인한다.
    """
    gross = sum(o.amount for o in plan.buys)
    if gross <= 0:
        return 0.0
    return round(gross * (1 + fee_rate + SAFETY_MARGIN), 2)
