"""
================================================================
리버스모드 (소진모드)

발동   T > 분할수 - 1  또는 일반모드에서 1회 매수가 불가능해진 시점
       남은 잔금이 있어도 리버스모드로 넘어가며, 그 잔금도 함께 쓴다.

첫날   MOC 매도만. 매수 없음.
       수량 = 전체보유 ÷ 10(20분할) 또는 ÷ 20(40분할), 내림.
       무조건 매도를 의도하므로 별지점 계산 없이 MOC(33) 사용.

--- 둘째날부터는 두 단계로 나뉜다 (라오어 2026-08-02 보강) ---

[1단계] 쿼터매수로 1주 이상 살 수 있는 동안
       별지점 = 직전 5거래일 종가의 평균 (일반모드 별지점과 정의가 다름)
       매도: 별지점에 LOC, 수량은 '직전 보유수량' ÷ 10 또는 ÷ 20, 내림
       매수: 잔금 ÷ 4 만큼 별지점 아래에서 쿼터매수
             (일반 매수와 형태 동일 — 본주문 + 폭락대비 1주씩)
       T값이 19나 39를 넘어가도 이 단계는 계속된다.

[2단계] 쿼터매수(잔금÷4)로 1주도 살 수 없어진 시점 = 리버스 소진
       매수 시도 없이 MOC 매도만 시행한다.

T값   20분할  매도 T×0.90 / 매수 T + (20-T)×0.25
      40분할  매도 T×0.95 / 매수 T + (40-T)×0.25

종료   종가가 평단 대비 -15%(TQQQ) / -20%(SOXL) 보다 커지는 것을
       확인한 이후 일반모드로 복귀. T값과 1회매수액 계산은 그대로 이어간다.
       하루 만에 끝나는 경우도 있다.
================================================================
"""

from __future__ import annotations

from typing import Optional

from core.star_point import (
    crash_buy_prices,
    reverse_exit_threshold,
    reverse_sell_qty,
    reverse_star_point,
    should_exit_reverse,
)
from core.t_calculator import FillKind
from kiwoom.constants import TradeType
from modes.base_mode import (
    BaseTradingMode,
    MarketSnapshot,
    OrderPlan,
    PlannedOrder,
    PositionState,
)

#: 폭락대비 LOC 매수 건수
CRASH_ORDER_COUNT = 5

#: 쿼터매수 비율 (잔금 ÷ 4)
QUARTER_BUY_RATIO = 0.25


class ReverseMode(BaseTradingMode):
    """리버스모드 주문 생성기"""

    # ------------------------------------------------------------
    # 소진 판정
    # ------------------------------------------------------------

    def quarter_buy_amount(self) -> float:
        """쿼터매수 금액 = 잔금 ÷ 4"""
        return max(0.0, self.state.cash) * QUARTER_BUY_RATIO

    def is_exhausted(self, buy_price: float) -> bool:
        """리버스 소진 판정 (라오어 2026-08-02 보강).

        쿼터매수(잔금÷4)로 1주도 살 수 없으면 소진이다.
        이 시점부터 매수 시도 없이 MOC 매도만 시행한다.
        T값이 19나 39를 넘었는지는 기준이 아니다.
        """
        if buy_price <= 0:
            return True
        return self.quarter_buy_amount() < buy_price

    # ------------------------------------------------------------
    # 계획
    # ------------------------------------------------------------

    def plan(self, market: MarketSnapshot) -> OrderPlan:
        st = self.state
        plan = OrderPlan(ticker=st.ticker, mode="reverse", T=st.T)

        if st.holdings <= 0:
            plan.warnings.append("보유수량이 0입니다. 리버스모드를 종료하고 사이클을 닫으세요.")
            return plan

        # 평단이 0이면 종료 판정(should_exit_reverse)이 항상 False 가 되어
        # 리버스모드에서 영영 빠져나오지 못한다. 보유가 있는데 평단이 0인
        # 것은 장부 이상이므로 주문을 내지 않고 알린다.
        if st.avg_price <= 0:
            plan.warnings.append(
                f"보유 {st.holdings}주인데 평단이 0입니다. 장부 이상이므로 "
                f"주문을 생성하지 않습니다. 증권사 잔고와 대조 후 /fix 로 보정하세요.")
            return plan

        if st.reverse_first_day:
            self._plan_moc_sell(plan, "리버스 첫날 무조건매도")
            return plan

        # 둘째날부터: 별지점이 필요하다
        try:
            star = reverse_star_point(market.recent_closes)
        except ValueError as e:
            plan.warnings.append(
                f"별지점(직전 5거래일 종가 평균)을 계산할 수 없어 주문을 생성하지 않습니다: {e}"
            )
            return plan

        plan.star_point = star.star
        plan.unit_amount = self.quarter_buy_amount()

        # 2단계: 리버스 소진 → 매수 시도 없이 MOC 매도만
        if self.is_exhausted(star.buy_price):
            plan.exhausted = True
            plan.warnings.append(
                f"[리버스 소진] 쿼터매수액 ${plan.unit_amount:,.2f} 로는 "
                f"{star.buy_price:.2f} 에서 1주도 살 수 없습니다. "
                f"매수 시도 없이 MOC 매도만 시행합니다."
            )
            self._plan_moc_sell(plan, "리버스 소진 후 MOC매도")
            return plan

        # 1단계: 별지점 LOC 매도 + 쿼터매수
        self._plan_loc_sell(plan, star)
        self._plan_quarter_buy(plan, star)
        return plan

    # ------------------------------------------------------------
    # MOC 매도 (첫날 / 리버스 소진 후 공통)
    # ------------------------------------------------------------

    def _plan_moc_sell(self, plan: OrderPlan, note_prefix: str) -> None:
        """수량 규칙은 LOC 매도와 동일하게 직전 보유수량의 10·20등분(내림)."""
        st = self.state
        qty = reverse_sell_qty(st.holdings, st.division)
        divisor = 10 if st.division == 20 else 20

        if qty < 1:
            plan.warnings.append(
                f"보유 {st.holdings}주를 {divisor}등분하면 0주라 매도를 생성하지 않습니다. "
                f"잔여 물량 처리를 수동으로 결정하세요."
            )
            return

        plan.orders.append(PlannedOrder(
            tag=FillKind.REVERSE_MOC_SELL, side="sell", trade_type=TradeType.MOC,
            qty=qty, price=None,
            note=f"{note_prefix} 보유 {st.holdings}÷{divisor} (내림)",
        ))

    # ------------------------------------------------------------
    # 둘째날 이후 — LOC 매도
    # ------------------------------------------------------------

    def _plan_loc_sell(self, plan: OrderPlan, star) -> None:
        """별지점 위에서 LOC 매도"""
        st = self.state
        qty = reverse_sell_qty(st.holdings, st.division)
        divisor = 10 if st.division == 20 else 20

        if qty < 1:
            plan.warnings.append(
                f"보유 {st.holdings}주를 {divisor}등분하면 0주라 매도를 생성하지 않습니다."
            )
            return

        plan.orders.append(PlannedOrder(
            tag=FillKind.REVERSE_SELL, side="sell", trade_type=TradeType.LOC,
            qty=qty, price=star.sell_price,
            note=f"리버스 매도 보유 {st.holdings}÷{divisor} (내림) · 별지점 위",
        ))

    # ------------------------------------------------------------
    # 둘째날 이후 — 쿼터매수
    # ------------------------------------------------------------

    def _plan_quarter_buy(self, plan: OrderPlan, star) -> None:
        """쿼터매수. 소진 판정은 plan() 에서 이미 통과한 상태다."""
        st = self.state
        unit = self.quarter_buy_amount()
        qty = int(unit / star.buy_price)

        plan.orders.append(PlannedOrder(
            tag=FillKind.REVERSE_QUARTER_BUY, side="buy", trade_type=TradeType.LOC,
            qty=qty, price=star.buy_price,
            note=f"쿼터매수 잔금 {st.cash:,.2f}÷4 · 별지점 아래",
        ))

        # 폭락대비 (일반모드와 동일한 형태).
        # 이미 계획에 있는 단가와 겹치면 건너뛴다 — 주문수량이 크면
        # 1회매수액을 나눈 값이 센트 단위에서 같아져, 같은 가격에 주문이
        # 여럿 생기고 체결 태그를 복원할 수 없게 된다.
        used = {o.price for o in plan.orders if o.side == "buy" and o.price is not None}
        skipped = 0
        for i, price in enumerate(crash_buy_prices(unit, qty, CRASH_ORDER_COUNT), start=1):
            if price <= 0:
                continue
            if price in used:
                skipped += 1
                continue
            used.add(price)
            plan.orders.append(PlannedOrder(
                tag=FillKind.CRASH_BUY, side="buy", trade_type=TradeType.LOC,
                qty=1, price=price,
                note=f"폭락대비 {i} ({unit:.2f}/{qty + i}) · T불변",
            ))
        if skipped:
            plan.warnings.append(
                f"폭락대비 {skipped}건이 기존 주문과 단가가 같아 생략했습니다.")

    # ------------------------------------------------------------
    # 모드 전환
    # ------------------------------------------------------------

    def check_exit(self, close_price: float) -> Optional[str]:
        """일반모드 복귀 판정.

        종가가 평단 대비 -15%(TQQQ)/-20%(SOXL) 보다 커지면 그 날부터
        일반모드로 돌아간다. T값은 리버스에서 쓰던 값을 그대로 이어 쓴다.
        """
        st = self.state
        if st.avg_price <= 0 or close_price <= 0:
            return None
        if should_exit_reverse(st.ticker, st.avg_price, close_price):
            return "normal"
        return None

    def exit_threshold(self) -> float:
        """일반모드 복귀 기준가 (텔레그램 표시용)"""
        return reverse_exit_threshold(self.state.ticker, self.state.avg_price)


# ================================================================
# 편의 함수
# ================================================================

def plan_reverse_day(state_dict: dict, market: MarketSnapshot) -> OrderPlan:
    """dict 기반 state 로 바로 계획 생성"""
    return ReverseMode(PositionState.from_dict(state_dict)).plan(market)


def should_enter_reverse(t: float, division: int) -> bool:
    """T 기준 소진 판정.

    일반모드에서 1회 매수가 불가능해진 경우도 소진이며,
    그쪽은 OrderPlan.exhausted 로 전달된다.
    """
    return t > (division - 1)
